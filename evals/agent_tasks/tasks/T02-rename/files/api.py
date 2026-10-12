import users


def handle(request):
    user = users.get_usr(request["id"])
    return {"status": 200 if user else 404, "user": user}
