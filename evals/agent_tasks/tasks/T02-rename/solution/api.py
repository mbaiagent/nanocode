import users


def handle(request):
    user = users.get_user(request["id"])
    return {"status": 200 if user else 404, "user": user}
