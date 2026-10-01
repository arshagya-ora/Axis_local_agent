"""Bounded source previews for orientation, separate from exhaustive read coverage."""
import json
import re


def spread(items, limit):
    """Include the beginning, middle and end, in source order."""
    if len(items) <= limit:
        return items
    return [items[round(i * (len(items)-1) / (limit-1))] for i in range(limit)]


class DocumentOverview:
    def saved_overview(self, identifier):
        if self._reading_restricted(identifier):
            return None
        key = self.scope_key(self.scope(identifier))
        with self.store.history.lock:
            row = self.store.history.db.execute('SELECT result FROM document_overviews WHERE task_id=? AND attachment_id=? AND scope=?',
                (self.task_id, identifier, key)).fetchone()
        return json.loads(row[0]) if row else None

    def overview(self, identifier):
        self._check_read(identifier)
        cached = self.saved_overview(identifier)
        if cached:
            return cached
        scope = self.scope(identifier)
        where, args, _ = self.store.section_filter(identifier, **scope)
        with self.store.history.lock:
            rows = self.store.history.db.execute(f'SELECT id,ordinal,location,sheet FROM attachment_sections WHERE {where} ORDER BY ordinal', args).fetchall()
        # Heading/page/slide/sheet metadata gives broad orientation without a
        # model call per 16 paragraphs. No excerpt is counted as a full read.
        groups = []
        for row in rows:
            location = row['location']
            heading = location.partition(' | ')[2].strip()
            label = row['sheet'] or heading or re.sub(r' \[characters .*|, (?:table row|notes).*', '', location)
            if groups and groups[-1]['label'] == label:
                groups[-1]['rows'].append(row)
            else:
                groups.append(dict(label=label, rows=[row]))
        chosen = spread(groups, 24)
        candidates = {r['id']: r for group in chosen for r in spread(group['rows'], 3)}
        # Add uniform source samples so sparse or missing headings cannot bias
        # the preview entirely toward the beginning of a long document.
        candidates.update({r['id']: r for r in spread(rows, 12)})
        selected = spread(sorted(candidates.values(), key=lambda r:r['ordinal']), 36)
        excerpts = []
        for row in selected:
            source = self.store.sections(self.task_id, identifier, ids=[row['id']], **scope)['items']
            if source:
                text = source[0]['text']
                excerpts.append(dict(section_id=row['id'], location=source[0]['location'][:200],
                    text=text[:320], truncated=len(text)>320))
        structure = [dict(label=g['label'][:140], first_section_id=g['rows'][0]['id'],
            last_section_id=g['rows'][-1]['id'], sections=len(g['rows'])) for g in spread(groups, 36)]
        result = dict(attachment_id=identifier, scope=scope, total_sections=len(rows),
            structure=structure, structure_groups=len(groups), structure_truncated=len(groups)>len(structure),
            excerpts=excerpts, sampled_sections=len(excerpts), exhaustive=False, overview_ready=True,
            note='Source excerpts sampled across the requested scope. Answer the overview now; disclose that this is an overview, not an exhaustive review. Do not claim every section or value was read. Use targeted reads only for a specific missing fact.')
        with self.store.history.transaction():
            self.store.history.db.execute('INSERT OR REPLACE INTO document_overviews VALUES(?,?,?,?)',
                (self.task_id, identifier, self.scope_key(scope), json.dumps(result)))
        return result
