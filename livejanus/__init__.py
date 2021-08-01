from flask import (
    redirect,
    render_template,
    request,
    Blueprint,
    make_response,
)
from os.path import join as pjoin, abspath, dirname
from livejanus.environ import environ
from livejanus.util import (
    random_string,
    time_as_utc,
    SocketInvalidDataException,
    alphanumeric,
)
from .db import EventUser, Record, StripeSession, User, db, Event
from .auth import auth_handler, socket_session_handler
from flask_socketio import SocketIO, join_room, emit
import stripe

blueprint_root = dirname(abspath(__file__))
livejanus = Blueprint(
    "livejanus",
    __name__,
    template_folder=pjoin(blueprint_root, "templates"),
    static_folder=pjoin(blueprint_root, "static"),
    url_prefix="",
    static_url_path="",
)
livejanus_socketio = SocketIO()

stripe.api_key = environ["STRIPE_PRIVATE_KEY"]


def make_logged_in_response(session_token: str, redirect_url: str):
    response = make_response(redirect(redirect_url))
    response.set_cookie(
        "session",
        session_token,
        max_age=None,
        secure=True,
        httponly=environ["DEBUG"] is not True,
        samesite="strict",
    )
    return response


@livejanus.route("/")
def page_splash():
    return render_template("splash.html")


@livejanus.route("/user/login/", methods=["GET", "POST"])
def page_user_login():
    if request.method == "GET":
        return render_template("user_login.html")
    session_token = User.authenticate(
        request.form["username"], request.form["password"]
    )
    if not session_token:
        error_msg = "The login attempt was invalid"
        if (
            User.query.filter(User.username == request.form["username"]).first()
            is not None
        ):
            error_msg += ". If you have lost access to your account, contact support at: livejanus@joedean.dev"
        return render_template("user_login.html", error_msg=error_msg)
    return make_logged_in_response(session_token, f"/user/")


@livejanus.route("/user/signup/", methods=["GET", "POST"])
def page_user_signup():
    if request.method == "GET":
        return render_template("signup.html")
    try:
        if len(request.form["username"]) < 3 or len(request.form["email"]) < 5:
            raise Exception
        user = User(
            request.form["username"], request.form["password"], request.form["email"]
        )
        db.session.add(user)
        db.session.commit()
    except:
        return render_template("signup.html", error_msg="The signup process failed.")
    session_token = User.authenticate(
        request.form["username"], request.form["password"]
    )
    return make_logged_in_response(session_token, f"/user/")


@livejanus.route("/user/", methods=["GET", "POST"])
def page_user_home():
    fail_response = redirect("/user/login/")

    if "stripe" in request.args:
        checkout_id = request.args.get("stripe")
        stripe_session = (
            StripeSession.query.filter(StripeSession.session == checkout_id)
            .filter(StripeSession.used == False)
            .first()
        )
        if stripe_session is None:
            return fail_response
        user = User.query.filter(User.id == stripe_session.user).first()
        if user is None:
            return fail_response
        stripe_session.used = True
        event = Event(user.id, "Premium Event", premium=True)
        db.session.add(event)
        db.session.commit()
        return render_template("stripe_confirm.html", event_key=event.key)

    if "session" not in request.cookies:
        return fail_response
    user: User = auth_handler.validate(request.cookies["session"])
    if user is None:
        return fail_response
    error_msg = None
    if (
        request.method == "POST"
        and "action" in request.form
        and request.form["action"] == "create"
    ):
        if "premium" in str(request.form["submit"]).lower():
            stripe_checkout_session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                line_items=[
                    {
                        "price": environ["STRIPE_PRICE_ID"],
                        "quantity": 1,
                    }
                ],
                mode="payment",
                success_url=request.host_url + "user/?stripe={CHECKOUT_SESSION_ID}",
                cancel_url=request.host_url + "user/",
            )
            db.session.add(
                StripeSession(
                    user.id, stripe_checkout_session.id, request.cookies["session"]
                )
            )
            db.session.commit()
            return redirect(stripe_checkout_session.url, code=303)
        else:
            if (
                Event.query.filter(Event.owner == user.id)
                .filter(Event.is_premium == False)
                .count()
                >= 5
            ):
                error_msg = "No more than 5 basic events can be created."
            else:
                db.session.add(Event(user.id, "Untitled Event"))
                db.session.commit()
    events = list(Event.query.filter(Event.owner == user.id))
    events.sort(key=lambda x: x.created_time)
    events.reverse()
    return render_template("user.html", user=user, events=events, error_msg=error_msg)


@livejanus.route("/user/<event_key>/", methods=["GET", "POST"])
def page_user_event(event_key):
    fail_response = redirect("/user/")
    if "session" not in request.cookies:
        return fail_response
    user: User = auth_handler.validate(request.cookies["session"])
    event = Event.from_key(event_key)
    if user is None or event is None or event.owner != user.id:
        return fail_response

    error_msgs = []
    if request.method == "POST":
        event.name = request.form["eventName"]
        if "eventUserNew" in request.form and len(request.form["eventUserNew"]) > 0:
            if (
                not event.is_premium
                and EventUser.query.filter(EventUser.event == event.id).count() > 2
            ):
                error_msgs.append("Basic events are limited to a maximum of 2 users.")
            else:
                db.session.add(
                    EventUser(
                        event.id, request.form["eventUserNew"], random_string(length=32)
                    )
                )

        if "eventMax" in request.form:
            event.max_value = int(request.form["eventMax"])

        for form_key in request.form.keys():
            if (
                str(form_key).startswith("eventUserPassword_")
                and len(request.form[form_key]) > 0
            ):
                changed_username = form_key[form_key.index("_") + 1 :]
                changed_user = (
                    EventUser.query.filter(EventUser.event == event.id)
                    .filter(EventUser.username == changed_username)
                    .first()
                )
                if changed_user is not None:
                    changed_user.set_password(request.form[form_key])
                else:
                    error_msgs.append("Invalid user for password change.")
        try:
            db.session.commit()
        except:
            db.session.rollback()
            error_msgs.append(
                "A database error occurred, and the changes were not saved"
            )
    event_users = list(
        EventUser.query.filter(EventUser.event == event.id).order_by(
            EventUser.created_time
        )
    )
    return render_template(
        "event.html",
        event=event,
        event_users=event_users,
        error_msg=" - ".join(error_msgs) if len(error_msgs) > 0 else None,
    )


@livejanus.route("/user/<event_key>/download/", methods=["GET"])
def page_event_download(event_key):
    user: User = auth_handler.validate(request.cookies["session"])
    event = Event.from_key(event_key)
    if user is None or event is None or event.owner != user.id:
        return redirect(f"/user/")
    if not event.is_premium:
        return redirect(f"/user/{event_key}/")

    filename = "".join(
        [
            _
            for _ in f"{event.name}_LiveJanus".replace(" ", "_")
            if _ in alphanumeric + "_"
        ]
    )

    response = make_response(event.create_csv())
    response.headers["Content-Disposition"] = f"attachment; filename={filename}.csv"
    response.headers["Content-type"] = "text/csv"
    return response


@livejanus.route("/event/login/", methods=["GET", "POST"])
def page_event_login():
    if request.method == "GET":
        return render_template("event_login.html")
    target_event = Event.from_key(request.form["key"])
    if target_event is None:
        return render_template(
            "event_login.html",
            error_msg=f"The key "
            f'"{request.form["key"] if len(request.form["key"]) > 1 else "..."}'
            f'" does not match any event',
        )
    if "username" not in request.form:
        return render_template("event_login.html", event_key=request.form["key"])
    target_user = (
        EventUser.query.filter(EventUser.event == target_event.id)
        .filter(EventUser.username == request.form["username"])
        .first()
    )
    session_token = EventUser.authenticate(
        request.form["username"], request.form["password"], target_event.id
    )
    if target_user is None or not session_token:
        return render_template(
            "event_login.html",
            error_msg=f"Invalid login attempt for {request.form['username']}",
            event_key=request.form["key"],
            username=request.form["username"],
        )
    return make_logged_in_response(session_token, f'/event/{request.form["key"]}')


@livejanus.route("/event/<event_key>/")
def page_event_counter(event_key):
    fail_response = redirect("/event/login")
    if "session" not in request.cookies:
        return fail_response
    event_user: EventUser = auth_handler.validate(request.cookies["session"])
    if event_user is None:
        return fail_response
    event = Event.from_key(event_key)
    if event is None:
        return fail_response
    if event_user.event != event.id:
        return fail_response

    if not event.is_happening:
        return render_template(
            "event_login.html",
            error_msg=f"The requested event is no longer available.",
            event_key=request.form["key"],
            username=request.form["username"],
        )
    return render_template(
        "counter.html",
        event=event,
        event_max=event.max_value if event.max_value is not None else -1,
        event_username=event_user.username,
    )


@livejanus_socketio.on("join")
def socket_join(data):
    try:
        event_user: EventUser = auth_handler.validate(data)
        if event_user is None:
            raise SocketInvalidDataException("The session cookie was invalid")
        event = Event.query.filter(Event.id == event_user.event).first()
        if event is None:
            raise SocketInvalidDataException("The event was not found")
        socket_session_handler.save(
            request.sid, event_user.username, event_user.id, event.key, event.id
        )
        join_room(event.key)
        emit("join", event.total_value)
    except Exception:
        emit("join", False)


@livejanus_socketio.on("update")
def socket_update(data):
    try:
        session_data = socket_session_handler.fetch(request.sid)
        if session_data is None:
            raise SocketInvalidDataException("The session ID was not found")
        event_user_name, event_user_id, event_key, event_id = session_data
        if data not in [1, -1]:
            raise SocketInvalidDataException(f'Update value "{data}" was invalid')
        event = Event.query.filter(Event.id == event_id).first()
        if not event.is_happening:
            raise SocketInvalidDataException("The event has ended")

        event = Event.query.filter(Event.id == event_id).first()
        event.add_record(event_user_id, data)
        emit(
            "update",
            [time_as_utc(), event_user_name, event.total_value, data],
            room=event_key,
        )
    except Exception:
        emit("update", False)
