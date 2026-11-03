def page_count(items, page_size):
    if page_size <= 0:
        raise ValueError("page_size must be positive")
    return len(items) // page_size
