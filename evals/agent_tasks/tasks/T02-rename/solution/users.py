_DB = {1: "alice", 2: "bob"}


def get_user(user_id):
    return _DB.get(user_id)
