"""Scoped document coverage and bounded, source-linked findings in SQLite."""
import json

from .scope import requested_range, normalized


class DocumentProgress:
    def scope(self, identifier, sheet=None, cell_range=None):
        use = self._uses().get(identifier, {})
        requested = requested_range(self.store, self.task_id, identifier)
        scope = {'sheet': sheet or use.get('sheet'), 'cell_range': cell_range or use.get('cell_range')}
        if requested:
            if scope['sheet'] and scope['sheet'] != requested['sheet']:
                raise ValueError('Read the sheet requested by the user: ' + requested['sheet'])
            if scope['cell_range']:
                from openpyxl.utils.cell import range_boundaries
                a, b, c, d = range_boundaries(scope['cell_range'])
                x, y, z, w = range_boundaries(requested['cell_range'])
                if not (x <= a <= c <= z and y <= b <= d <= w):
                    raise ValueError('Read within the requested range: ' + requested['cell_range'])
            scope = {'sheet': requested['sheet'], 'cell_range': scope['cell_range'] or requested['cell_range']}
        return scope

    @staticmethod
    def scope_key(scope):
        return json.dumps(scope, sort_keys=True)

    def coverage(self):
        result = []
        with self.store.history.lock:
            db = self.store.history.db
            full_reads = {r[0] for r in db.execute('SELECT section_id FROM document_reads WHERE task_id=?', (self.task_id,))}
            scoped_reads = [(r['section_id'], r['scope'], json.loads(r['scope'])) for r in db.execute('SELECT section_id,scope FROM document_scope_reads WHERE task_id=?', (self.task_id,))]
            for item in self.store.catalog(self.task_id):
                scope = self.scope(item['id'])
                key = self.scope_key(scope)
                where, args, _ = self.store.section_filter(item['id'], **scope)
                rows = db.execute(f'SELECT id,ordinal,location FROM attachment_sections WHERE {where} ORDER BY ordinal', args).fetchall()
                read = set(full_reads)
                intervals = {}
                for section_id, recorded_key, recorded in scoped_reads:
                    if recorded_key == key:
                        read.add(section_id)
                    elif scope['cell_range'] and recorded.get('cell_range') and recorded.get('sheet') == scope['sheet']:
                        from openpyxl.utils.cell import range_boundaries
                        a, _, b, _ = range_boundaries(recorded['cell_range'])
                        intervals.setdefault(section_id, []).append((a, b))
                if scope['cell_range']:
                    from openpyxl.utils.cell import range_boundaries
                    x, _, y, _ = range_boundaries(scope['cell_range'])
                    for section_id, chunks in intervals.items():
                        next_column = x
                        for a, b in sorted(chunks):
                            if a > next_column:
                                break
                            next_column = max(next_column, b+1)
                        if next_column > y:
                            read.add(section_id)
                missing = [dict(row) for row in rows if row['id'] not in read]
                scanned = db.execute('SELECT 1 FROM document_scans WHERE task_id=? AND attachment_id=? AND scope=?', (self.task_id, item['id'], key)).fetchone()
                result.append(dict(attachment_id=item['id'], filename=item['filename'], **scope,
                    purpose=self._uses().get(item['id'], {}).get('purpose'), required_coverage=self._uses().get(item['id'], {}).get('coverage'),
                    total=len(rows), read=len(rows)-len(missing), complete=bool(rows or scanned) and not missing,
                    next_section_ids=[row['id'] for row in missing[:16]], remaining=len(missing)))
        return result

    def notes(self, offset=0, identifier=None, limit=24, max_chars=12000):
        with self.store.history.lock:
            where = 'n.task_id=?'
            args = [self.task_id]
            if identifier:
                self.store.require(self.task_id, identifier)
                where += ' AND s.attachment_id=?'
                args.append(identifier)
            rows = self.store.history.db.execute(f'''SELECT n.section_id,n.quote,s.attachment_id,s.location
                FROM document_notes n JOIN attachment_sections s ON s.id=n.section_id
                WHERE {where} ORDER BY s.attachment_id,s.ordinal,n.quote LIMIT ? OFFSET ?''', (*args, limit+1, offset)).fetchall()
        items, size, examined = [], 0, 0
        for row in rows[:limit]:
            item = dict(row)
            if items and size + len(json.dumps(item)) > max_chars:
                break
            examined += 1
            if self._reading_restricted(item['attachment_id']):
                continue
            scope = self.scope(item['attachment_id'])
            if scope.get('cell_range') or scope.get('sheet'):
                source = self.store.sections(self.task_id, item['attachment_id'], ids=[item['section_id']], **scope)
                if not source['items'] or normalized(item['quote']) not in normalized(source['items'][0]['text']):
                    continue
            items.append(item)
            size += len(json.dumps(item))
        return dict(items=items, has_more=len(rows)>examined, next_offset=offset+examined)

    def retain_notes(self, notes):
        with self.store.history.transaction():
            for note in notes:
                row = self.store.history.db.execute('SELECT attachment_id FROM attachment_sections WHERE id=?', (note.section_id,)).fetchone()
                if not row:
                    raise ValueError('Unknown section for document finding.')
                self._check_read(row[0])
                scope = self.scope(row[0])
                recorded = self.store.history.db.execute('SELECT scope FROM document_scope_reads WHERE task_id=? AND section_id=?', (self.task_id, note.section_id)).fetchall()
                sources = [self.store.sections(self.task_id, row[0], ids=[note.section_id], **json.loads(r[0])) for r in recorded]
                if not sources:
                    raise ValueError('Read the section in the current scope before retaining a finding.')
                result = self.store.sections(self.task_id, row[0], ids=[note.section_id], **scope)
                if (not result['items'] or normalized(note.quote) not in normalized(result['items'][0]['text']) or
                        not any(source['items'] and normalized(note.quote) in normalized(source['items'][0]['text']) for source in sources)):
                    raise ValueError('Document findings must quote the read source text.')
                self.store.history.db.execute('INSERT OR IGNORE INTO document_notes VALUES(?,?,?)', (self.task_id, note.section_id, note.quote))

    def checkpoint(self, state, limits=None):
        """Save compact control state; raw source content already lives in storage."""
        with self.store.history.transaction():
            task = self.store.history.task(self.task_id)
            # Source text stays private, out of every public task/event payload.
            self.store.history.db.execute('INSERT INTO document_checkpoints VALUES(?,?) ON CONFLICT(task_id) DO UPDATE SET last_result=excluded.last_result',
                (self.task_id, json.dumps(self.last_result)))
            task['document_checkpoint'] = dict(
                base_limits=limits or task.get('document_checkpoint', {}).get('base_limits', task.get('limits', {})),
                remaining_work=state.remaining_work, total_steps=state.counters.total_steps,
                sequence=state.sequence, evidence_number=state.evidence_number, goal_number=state.goal_number,
                pending_uploads=state.pending_uploads,
                pending_changes={key:value.model_dump(mode='json') for key,value in state.pending_changes.items()},
                current_goal_id=state.current_goal_id, current_goal=state.current_goal,
                current_verification=state.current_verification.model_dump(mode='json') if state.current_verification else None,
                assertions=[value.model_dump(mode='json') for value in state.assertions],
                model_requests=int(state.usage.requests), browser_actions=state.counters.browser_actions,
                mutation_count=state.counters.mutation_count, verified_mutation_count=state.counters.verified_mutation_count,
                successful_actions=state.successful_actions,
                input_tokens=int(state.usage.input_tokens), output_tokens=int(state.usage.output_tokens),
                extra_steps=state.extra_steps, extra_model_requests=state.extra_model_requests,
                extra_browser_actions=state.extra_browser_actions,
                last_error=state.last_error,
                auto_steps=state.document_auto_steps,
                extension_progress=state.document_extension_progress)
            self.store.history.save_task(task)
