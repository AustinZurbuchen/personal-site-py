def sort_work_items(work_items):
    """Order work experience current-first, then most recent first.

    Every key is coerced to a string on purpose. The sort compares tuples
    element by element, so a single bad value raises TypeError, /getResume
    returns 500, and the whole public site paints the error band — from one
    field in one work item.

    `item.get('endDate', '')` is NOT sufficient: the default only applies when
    the key is ABSENT. A key present with a JSON null yields None, and
    comparing None to a str raises. An int year vs a string date raises too.
    Both are natural ways to hand-edit a document in Atlas, and neither
    endDate, startDate nor isCurrent renders anywhere in the UI, so a bad edit
    gives no feedback until the site goes dark.
    """
    if not isinstance(work_items, list):
        return work_items

    def key(item):
        if not isinstance(item, dict):
            return (0, '', '')
        return (
            1 if item.get('isCurrent') else 0,
            str(item.get('endDate') or ''),
            str(item.get('startDate') or ''),
        )

    return sorted(work_items, key=key, reverse=True)
