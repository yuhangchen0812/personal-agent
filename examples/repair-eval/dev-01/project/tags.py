def normalize_tag(text):
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    return text.lower()
