from users import get_user


def greeting(user_id):
    name = get_user(user_id)
    return f"hello, {name}" if name else "who are you?"
