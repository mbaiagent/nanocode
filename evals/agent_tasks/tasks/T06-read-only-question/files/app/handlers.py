from app.audit import audit


def delete_user(user_id):
    audit("delete_user", user_id)
    return True
