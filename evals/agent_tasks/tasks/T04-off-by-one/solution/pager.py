def paginate(items, page, per_page):
    start = (page - 1) * per_page
    return items[start:start + per_page]


def total_pages(count, per_page):
    return (count + per_page - 1) // per_page
