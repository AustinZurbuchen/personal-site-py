def sort_work_items(work_items):
    def key(item):
        is_current = item.get('isCurrent', False)
        end_date = item.get('endDate', '')
        start_date = item.get('startDate', '')
        return (1 if is_current else 0, end_date, start_date)
    return sorted(work_items, key=key, reverse=True)