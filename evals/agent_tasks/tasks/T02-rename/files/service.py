from users import get_usr


def greeting(user_id):
    name = get_usr(user_id)
    return f"hello, {name}" if name else "who are you?"
