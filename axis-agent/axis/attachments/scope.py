"""Attachment constraints come from user messages, never file roles or contents."""
from __future__ import annotations

import re


def normalized(text):
    return ' '.join(text.casefold().split())


def overview_request(text):
    """Recognize broad orientation questions, not exhaustive extraction requests."""
    text = normalized(text)
    if re.search(r'\b(?:exhaustive|comprehensive|detailed|verbatim)\b|\b(?:all|every|each)\s+(?:section|paragraph|page|row|cell|scenario|entry|item)', text):
        return False
    return bool(re.search(r'\boverview\b|\b(?:brief|quick|short|high.level)\s+summary\b|'
        r'\bwhat\s+(?:is|was|s)\s+(?:written|present|contained)\b|'
        r'\bwhat\s+(?:does|do)\b.+\b(?:contain|say|cover|describe)\b|'
        r'\bwhat\b.+\b(?:file|document|attachment)\b.+\babout\b', text))


def user_messages(store, task_id):
    with store.history.lock:
        rows = store.history.db.execute(
            "SELECT text FROM messages WHERE task_id=? AND role='user' AND delivery='applied' "
            "AND intent IN ('new_task','follow_up') ORDER BY ordinal", (task_id,)).fetchall()
    task = store.history.task(task_id)
    original = task.get('instruction', '').split('\nFollow-up:', 1)[0]
    messages = [row[0] for row in rows]
    if original and (not messages or messages[0] != original):
        messages.insert(0, original)
    return messages


def constraints(store, task_id, identifier):
    """Apply successive, file-scoped user clauses. Reading and summary differ.

    This is a conservative guard for explicit prohibitions; the planner still
    interprets the requested output and declares its structured source scope.
    Legacy UI roles have no authorization meaning.
    """
    store.require(task_id, identifier)
    catalog = store.catalog(task_id)
    filename = store.get(identifier)['filename'].casefold()
    result = {'read': True, 'summarize': True, 'upload': True, 'execute': False}
    verbs = {
        'read': r'read(?:ing)?|inspect(?:ing)?',
        'summarize': r'summari[sz](?:e|ing)|analy[sz](?:e|ing)',
        'upload': r'upload(?:ing)?|attach(?:ing)?',
        'execute': r'follow|execute|configure|apply|perform|implement|carry out',
    }
    for message in user_messages(store, task_id):
        # Keep filename dots intact. A named file applies to its sentence;
        # an unqualified grant across multiple files must explicitly say both/all.
        for sentence in re.split(r'(?<=[.!?])\s+|[;\n]', message.casefold()):
            named = [item['filename'].casefold() for item in catalog if item['filename'].casefold() in sentence]
            if named and filename not in named:
                continue
            for clause in re.split(r'\b(?:but|then|however)\b', sentence):
                negative = re.search(r"\b(?:do not|don't|never|must not)\s+", clause)
                for operation, verb in verbs.items():
                    if re.search(rf"\b(?:do not|don't|never|must not)\s+(?:{verb})\b", clause):
                        result[operation] = False
                    elif not negative and (named or len(catalog) == 1 or re.search(r'\b(both|all|every)\b', clause)):
                        if re.search(rf'(?:(?:^|\band\s+)(?:please\s+)?(?:now\s+)?|\b(?:allow|permit|you may|you can now|go ahead and)\s+)(?:{verb})\b', clause):
                            result[operation] = True
                        elif operation == 'execute' and re.search(r'\buse\b.+\bto\s+(?:configure|apply|execute)\b', clause):
                            result[operation] = True
    return result


def requested_range(store, task_id, identifier):
    """Recognize explicit spreadsheet boundaries without inventing a sheet.

    Other natural-language selections are handled by the planner's typed scope.
    The common finite-range forms are also enforced below the planner.
    """
    from openpyxl.utils.cell import get_column_letter
    filename = store.get(identifier)['filename'].casefold()
    catalog = store.catalog(task_id)
    with store.history.lock:
        sheets = [r[0] for r in store.history.db.execute('SELECT DISTINCT sheet FROM attachment_sections WHERE attachment_id=? AND sheet IS NOT NULL', (identifier,))]
    if not sheets:
        return {}
    selected = {}
    for message in user_messages(store, task_id):
        names = [a['filename'].casefold() for a in catalog if a['filename'].casefold() in message.casefold()]
        if names and filename not in names:
            continue
        if len(catalog) > 1 and not names:
            continue
        if re.search(r'^\s*(?:please\s+)?(?:now\s+)?(?:read|inspect|summari[sz]e)\b.*?\b(?:whole|entire|all)\b.*?\b(?:workbook|file|sheet|rows)\b', message, re.I):
            selected = {}
        exact = None
        for name in sorted(sheets, key=len, reverse=True):
            match = re.search(rf"(?<!\w)'?{re.escape(name)}'?\s*!\s*([A-Z]+\d+:[A-Z]+\d+)", message, re.I)
            if match:
                exact = (name, match[1].upper())
                break
        sheet = re.search(r'\b(?:from|in|of)\s+(?:the\s+)?[\"\']?(.+?)[\"\']?\s+sheet\b', message, re.I)
        rows = re.search(r'\brows?\s+(\d+)\s*(?:through|to|[-–])\s*(\d+)\b', message, re.I)
        cols = re.search(r'\bcolumns?\s+([A-Z]+)\s*(?:through|to|[-–])\s*([A-Z]+)\b', message, re.I)
        if exact:
            selected = {'sheet': exact[0], 'cell_range': exact[1]}
        elif rows:
            sheet_name = sheet[1].strip() if sheet else sheets[0] if len(sheets) == 1 else None
            if sheet_name:
                selected = {'sheet': sheet_name, 'cell_range': f'{cols[1].upper() if cols else "A"}{rows[1]}:{cols[2].upper() if cols else get_column_letter(1000)}{rows[2]}'}
    return selected
