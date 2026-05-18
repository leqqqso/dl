import os
import io
import time
import base64
from datetime import date
from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import or_, func
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import pyotp
import qrcode

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

app = Flask(__name__)
app.secret_key = "change-me-in-production"
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///mysite.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = os.path.join(app.static_folder, "uploads")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

db = SQLAlchemy(app)


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


follows = db.Table(
    "follows",
    db.Column("follower_id", db.Integer, db.ForeignKey("user.id"), primary_key=True),
    db.Column("followed_id", db.Integer, db.ForeignKey("user.id"), primary_key=True),
)


class User(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    username     = db.Column(db.String(80), unique=True, nullable=False)
    password     = db.Column(db.String(256), nullable=False)
    avatar       = db.Column(db.String(256), nullable=True)
    totp_secret  = db.Column(db.String(32),  nullable=True)
    totp_enabled = db.Column(db.Boolean, default=False, nullable=False)
    is_admin     = db.Column(db.Boolean, default=False, nullable=False)
    banner_color = db.Column(db.String(7),  nullable=True)
    points       = db.Column(db.Integer,    default=0,   nullable=False)
    posts        = db.relationship("Post", backref="author", lazy=True)

    following = db.relationship(
        "User", secondary=follows,
        primaryjoin=(follows.c.follower_id == id),
        secondaryjoin=(follows.c.followed_id == id),
        backref=db.backref("followers", lazy="dynamic"),
        lazy="dynamic",
    )

    def set_password(self, raw):
        self.password = generate_password_hash(raw, method="pbkdf2:sha256")

    def check_password(self, raw):
        return check_password_hash(self.password, raw)

    def is_following(self, user):
        return self.following.filter_by(id=user.id).count() > 0


class Message(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    sender_id   = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    receiver_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    body        = db.Column(db.String(1000), nullable=False)
    created_at  = db.Column(db.DateTime, server_default=db.func.now())
    is_read     = db.Column(db.Boolean, default=False, nullable=False)

    sender   = db.relationship("User", foreign_keys=[sender_id])
    receiver = db.relationship("User", foreign_keys=[receiver_id])


class Post(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    body       = db.Column(db.String(500), nullable=True)
    image      = db.Column(db.String(256), nullable=True)
    created_at = db.Column(db.DateTime, server_default=db.func.now())
    user_id    = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)


CHALLENGE_CATEGORIES = ["Fitness", "Learning", "Creative", "Social", "Other"]

challenge_participants = db.Table(
    "challenge_participants",
    db.Column("challenge_id", db.Integer, db.ForeignKey("challenge.id"), primary_key=True),
    db.Column("user_id",      db.Integer, db.ForeignKey("user.id"),       primary_key=True),
    db.Column("completed",    db.Boolean, default=False, nullable=False),
    db.Column("proof",        db.String(256), nullable=True),
)


class Challenge(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    title       = db.Column(db.String(120), nullable=False)
    description = db.Column(db.String(500), nullable=False)
    category    = db.Column(db.String(50),  nullable=False, default="Other")
    creator_id  = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at  = db.Column(db.DateTime, server_default=db.func.now())
    deadline    = db.Column(db.Date, nullable=True)
    creator     = db.relationship("User", backref="challenges")
    participants = db.relationship("User", secondary=challenge_participants,
                                   backref=db.backref("joined_challenges", lazy="dynamic"),
                                   lazy="dynamic")

    def is_joined(self, user):
        return self.participants.filter_by(id=user.id).count() > 0

    def is_completed_by(self, user):
        row = db.session.execute(
            challenge_participants.select().where(
                (challenge_participants.c.challenge_id == self.id) &
                (challenge_participants.c.user_id == user.id)
            )
        ).first()
        return bool(row and row.completed)

    def proof_for(self, user):
        row = db.session.execute(
            challenge_participants.select().where(
                (challenge_participants.c.challenge_id == self.id) &
                (challenge_participants.c.user_id == user.id)
            )
        ).first()
        return row.proof if row else None

    def reaction_count(self, kind):
        return ChallengeReaction.query.filter_by(challenge_id=self.id, reaction=kind).count()

    def user_reaction(self, user, kind):
        return ChallengeReaction.query.filter_by(
            challenge_id=self.id, user_id=user.id, reaction=kind).first() is not None


class CompletionLike(db.Model):
    __tablename__ = "completion_like"
    id                = db.Column(db.Integer, primary_key=True)
    challenge_id      = db.Column(db.Integer, db.ForeignKey("challenge.id"), nullable=False)
    completed_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    liker_id          = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    __table_args__ = (db.UniqueConstraint("challenge_id", "completed_user_id", "liker_id"),)


class ChallengeReaction(db.Model):
    __tablename__ = "challenge_reaction"
    id           = db.Column(db.Integer, primary_key=True)
    challenge_id = db.Column(db.Integer, db.ForeignKey("challenge.id"), nullable=False)
    user_id      = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    reaction     = db.Column(db.String(10), nullable=False)
    __table_args__ = (db.UniqueConstraint("challenge_id", "user_id", "reaction"),)


ACHIEVEMENT_DEFS = {
    "registered":       {"label": "New Member",          "icon": "&#127881;", "points": 1},
    "first_challenge":  {"label": "Challenge Creator",   "icon": "&#127942;", "points": 5},
    "first_completion": {"label": "Challenge Completer", "icon": "&#9989;",   "points": 10},
}


class Achievement(db.Model):
    __tablename__ = "achievement"
    id        = db.Column(db.Integer, primary_key=True)
    user_id   = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    kind      = db.Column(db.String(50), nullable=False)
    earned_at = db.Column(db.DateTime, server_default=db.func.now())
    __table_args__ = (db.UniqueConstraint("user_id", "kind"),)


class ForumTopic(db.Model):
    __tablename__ = "forum_topic"
    id         = db.Column(db.Integer, primary_key=True)
    title      = db.Column(db.String(200), nullable=False)
    body       = db.Column(db.String(5000), nullable=False)
    user_id    = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, server_default=db.func.now())
    author     = db.relationship("User", backref="forum_topics")
    comments   = db.relationship("ForumComment", backref="topic", lazy="dynamic",
                                 cascade="all, delete-orphan")


class ForumComment(db.Model):
    __tablename__ = "forum_comment"
    id         = db.Column(db.Integer, primary_key=True)
    body       = db.Column(db.String(5000), nullable=False)
    user_id    = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    topic_id   = db.Column(db.Integer, db.ForeignKey("forum_topic.id"), nullable=False)
    created_at = db.Column(db.DateTime, server_default=db.func.now())
    author     = db.relationship("User", backref="forum_comments")


def grant_achievement(user, kind):
    if Achievement.query.filter_by(user_id=user.id, kind=kind).first():
        return
    pts = ACHIEVEMENT_DEFS[kind]["points"]
    db.session.add(Achievement(user_id=user.id, kind=kind))
    user.points = (user.points or 0) + pts


with app.app_context():
    db.create_all()
    with db.engine.connect() as conn:
        cols = [r[1] for r in conn.execute(db.text("PRAGMA table_info(user)"))]
        if "avatar" not in cols:
            conn.execute(db.text("ALTER TABLE user ADD COLUMN avatar VARCHAR(256)"))
            conn.commit()
        ch_cols = [r[1] for r in conn.execute(db.text("PRAGMA table_info(challenge)"))]
        if "deadline" not in ch_cols:
            conn.execute(db.text("ALTER TABLE challenge ADD COLUMN deadline DATE"))
            conn.commit()
        cp_cols = [r[1] for r in conn.execute(db.text("PRAGMA table_info(challenge_participants)"))]
        if "proof" not in cp_cols:
            conn.execute(db.text("ALTER TABLE challenge_participants ADD COLUMN proof VARCHAR(256)"))
            conn.commit()
        if "totp_secret" not in cols:
            conn.execute(db.text("ALTER TABLE user ADD COLUMN totp_secret VARCHAR(32)"))
            conn.commit()
        if "totp_enabled" not in cols:
            conn.execute(db.text("ALTER TABLE user ADD COLUMN totp_enabled BOOLEAN NOT NULL DEFAULT 0"))
            conn.commit()
        if "is_admin" not in cols:
            conn.execute(db.text("ALTER TABLE user ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT 0"))
            conn.commit()
        post_cols = [r[1] for r in conn.execute(db.text("PRAGMA table_info(post)"))]
        if "image" not in post_cols:
            conn.execute(db.text("ALTER TABLE post ADD COLUMN image VARCHAR(256)"))
            conn.commit()
        if "body_nullable" not in post_cols:
            try:
                conn.execute(db.text("UPDATE post SET body='' WHERE body IS NULL"))
                conn.commit()
            except Exception:
                pass
        if "banner_color" not in cols:
            conn.execute(db.text("ALTER TABLE user ADD COLUMN banner_color VARCHAR(7)"))
            conn.commit()
        if "points" not in cols:
            conn.execute(db.text("ALTER TABLE user ADD COLUMN points INTEGER NOT NULL DEFAULT 0"))
            conn.commit()
        # Retroactively award 'registered' to existing users
        for u in User.query.all():
            if not Achievement.query.filter_by(user_id=u.id, kind="registered").first():
                db.session.add(Achievement(user_id=u.id, kind="registered"))
                u.points = (u.points or 0) + ACHIEVEMENT_DEFS["registered"]["points"]
        db.session.commit()
        # Auto-promote first user if no admin exists
        admin_exists = conn.execute(db.text("SELECT 1 FROM user WHERE is_admin=1 LIMIT 1")).first()
        if not admin_exists:
            conn.execute(db.text("UPDATE user SET is_admin=1 WHERE id=(SELECT MIN(id) FROM user)"))
            conn.commit()


def save_drawing(data_url, user_id):
    if data_url and data_url.startswith("data:image/png;base64,"):
        img_bytes = base64.b64decode(data_url.split(",", 1)[1])
        filename = f"graffiti_{user_id}_{int(time.time()*1000)}.png"
        with open(os.path.join(app.config["UPLOAD_FOLDER"], filename), "wb") as f:
            f.write(img_bytes)
        return filename
    return None


def make_qr_b64(secret, username):
    uri = pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name="MyApp")
    qr = qrcode.QRCode(version=1, box_size=6, border=4)
    qr.add_data(uri)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def get_viewer():
    me = session.get("username")
    return User.query.filter_by(username=me).first() if me else None


@app.context_processor
def inject_unread():
    username = session.get("username")
    if username:
        user = User.query.filter_by(username=username).first()
        if user:
            count = Message.query.filter_by(receiver_id=user.id, is_read=False).count()
            return {"unread_count": count, "viewer_is_admin": user.is_admin}
    return {"unread_count": 0, "viewer_is_admin": False}



@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username or not password:
            flash("Please fill in all fields.", "warning")
            return render_template("login.html")

        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            if user.totp_enabled:
                session["2fa_pending"] = username
                return redirect(url_for("login_2fa"))
            session["username"] = username
            return redirect(url_for("user_page"))

        flash("Invalid username or password.", "danger")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm", "")

        if not username or not password or not confirm:
            flash("Please fill in all fields.", "warning")
            return render_template("register.html")

        if password != confirm:
            flash("Passwords do not match.", "danger")
            return render_template("register.html")

        if User.query.filter_by(username=username).first():
            flash("Username already taken.", "danger")
            return render_template("register.html")

        user = User(username=username)
        user.set_password(password)
        db.session.add(user)
        db.session.flush()
        grant_achievement(user, "registered")
        db.session.commit()

        session["username"] = username
        return redirect(url_for("user_page"))

    return render_template("register.html")


@app.route("/feed", methods=["GET", "POST"])
def feed():
    if request.method == "POST":
        viewer = get_viewer()
        if not viewer:
            return redirect(url_for("login"))
        body = request.form.get("body", "").strip()
        drawing_data = request.form.get("drawing_data", "")
        if not body and not drawing_data:
            return redirect(url_for("feed"))
        drawing = save_drawing(drawing_data, viewer.id)
        db.session.add(Post(body=body or None, image=drawing, user_id=viewer.id))
        db.session.commit()
        return redirect(url_for("feed"))

    posts = Post.query.order_by(Post.created_at.desc()).limit(50).all()
    return render_template("feed.html", posts=posts, username=session.get("username"))


@app.route("/user", methods=["GET", "POST"])
def user_page():
    user = get_viewer()
    if not user:
        return redirect(url_for("login"))
    if request.method == "POST":
        body = request.form.get("body", "").strip()
        drawing_data = request.form.get("drawing_data", "")
        if body or drawing_data:
            drawing = save_drawing(drawing_data, user.id)
            db.session.add(Post(body=body or None, image=drawing, user_id=user.id))
            db.session.commit()
        return redirect(url_for("user_page"))
    posts = Post.query.filter_by(user_id=user.id).order_by(Post.created_at.desc()).all()
    achievements = Achievement.query.filter_by(user_id=user.id).all()
    return render_template("user.html", username=user.username, posts=posts,
                           user=user, avatar=user.avatar,
                           followers_count=user.followers.count(),
                           following_count=user.following.count(),
                           achievements=achievements,
                           achievement_defs=ACHIEVEMENT_DEFS)


@app.route("/upload-avatar", methods=["POST"])
def upload_avatar():
    user = get_viewer()
    if not user:
        return redirect(url_for("login"))

    file = request.files.get("avatar")
    if not file or file.filename == "":
        flash("No file selected.", "warning")
        return redirect(url_for("user_page"))

    if not allowed_file(file.filename):
        flash("Only image files are allowed (png, jpg, gif, webp).", "danger")
        return redirect(url_for("user_page"))

    ext = secure_filename(file.filename).rsplit(".", 1)[1].lower()
    filename = f"avatar_{user.id}.{ext}"
    file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))

    user.avatar = filename
    db.session.commit()
    return redirect(url_for("user_page"))


@app.route("/u/<username>")
def profile(username):
    user    = User.query.filter_by(username=username).first_or_404()
    posts   = Post.query.filter_by(user_id=user.id).order_by(Post.created_at.desc()).all()
    viewer  = get_viewer()
    is_own  = viewer and viewer.id == user.id
    is_following = viewer.is_following(user) if viewer and not is_own else False
    achievements = Achievement.query.filter_by(user_id=user.id).all()
    return render_template("profile.html", user=user, posts=posts,
                           is_own=is_own, is_following=is_following,
                           followers_count=user.followers.count(),
                           following_count=user.following.count(),
                           achievements=achievements,
                           achievement_defs=ACHIEVEMENT_DEFS)


@app.route("/u/<username>/followers")
def profile_followers(username):
    user   = User.query.filter_by(username=username).first_or_404()
    return render_template("user_list.html", user=user, viewer=get_viewer(),
                           list_type="followers", users=user.followers.all())


@app.route("/u/<username>/following")
def profile_following(username):
    user   = User.query.filter_by(username=username).first_or_404()
    return render_template("user_list.html", user=user, viewer=get_viewer(),
                           list_type="following", users=user.following.all())


@app.route("/messages")
def messages():
    user = get_viewer()
    if not user:
        return redirect(url_for("login"))
    all_msgs = (Message.query
                .filter(or_(Message.sender_id == user.id,
                            Message.receiver_id == user.id))
                .order_by(Message.created_at.desc())
                .all())
    convos = {}
    for msg in all_msgs:
        other = msg.receiver if msg.sender_id == user.id else msg.sender
        if other.id not in convos:
            convos[other.id] = {"user": other, "last": msg, "unread": 0}
        if msg.receiver_id == user.id and not msg.is_read:
            convos[other.id]["unread"] += 1
    convos = sorted(convos.values(), key=lambda c: c["last"].created_at, reverse=True)
    return render_template("messages.html", convos=convos)


@app.route("/messages/<username>", methods=["GET", "POST"])
def conversation(username):
    me_user = get_viewer()
    if not me_user:
        return redirect(url_for("login"))
    other = User.query.filter_by(username=username).first_or_404()
    if request.method == "POST":
        body = request.form.get("body", "").strip()
        if body:
            db.session.add(Message(sender_id=me_user.id,
                                   receiver_id=other.id, body=body))
            db.session.commit()
        return redirect(url_for("conversation", username=username))
    msgs = (Message.query
            .filter(or_(
                (Message.sender_id == me_user.id) & (Message.receiver_id == other.id),
                (Message.sender_id == other.id)   & (Message.receiver_id == me_user.id),
            ))
            .order_by(Message.created_at.asc())
            .all())
    for m in msgs:
        if m.receiver_id == me_user.id and not m.is_read:
            m.is_read = True
    db.session.commit()
    return render_template("conversation.html", other=other, msgs=msgs, me_id=me_user.id)


@app.route("/follow/<username>", methods=["POST"])
def follow(username):
    viewer = get_viewer()
    if not viewer:
        return redirect(url_for("login"))
    target = User.query.filter_by(username=username).first_or_404()
    if viewer.id != target.id and not viewer.is_following(target):
        viewer.following.append(target)
        db.session.commit()
    return redirect(url_for("profile", username=username))


@app.route("/unfollow/<username>", methods=["POST"])
def unfollow(username):
    viewer = get_viewer()
    if not viewer:
        return redirect(url_for("login"))
    target = User.query.filter_by(username=username).first_or_404()
    if viewer.is_following(target):
        viewer.following.remove(target)
        db.session.commit()
    return redirect(url_for("profile", username=username))


@app.route("/")
def welcome():
    recent = Challenge.query.order_by(Challenge.created_at.desc()).limit(3).all()
    return render_template("welcome.html", viewer=get_viewer(), recent=recent)


@app.route("/challenges", methods=["GET", "POST"])
def challenges():
    me = session.get("username")
    if request.method == "POST":
        if not me:
            return redirect(url_for("login"))
        title    = request.form.get("title", "").strip()
        desc     = request.form.get("description", "").strip()
        category = request.form.get("category", "Other")
        if not title or not desc:
            flash("Title and description are required.", "warning")
            return redirect(url_for("challenges"))
        if category not in CHALLENGE_CATEGORIES:
            category = "Other"
        raw_deadline = request.form.get("deadline", "").strip()
        deadline = None
        if raw_deadline:
            try:
                deadline = date.fromisoformat(raw_deadline)
                if deadline <= date.today():
                    flash("Deadline must be a future date.", "warning")
                    return redirect(url_for("challenges"))
            except ValueError:
                pass
        user = User.query.filter_by(username=me).first()
        is_first = Challenge.query.filter_by(creator_id=user.id).count() == 0
        db.session.add(Challenge(title=title, description=desc,
                                 category=category, creator_id=user.id,
                                 deadline=deadline))
        if is_first:
            db.session.flush()
            grant_achievement(user, "first_challenge")
        db.session.commit()
        return redirect(url_for("challenges"))

    cat_filter = request.args.get("cat", "")
    q = Challenge.query.order_by(Challenge.created_at.desc())
    if cat_filter and cat_filter in CHALLENGE_CATEGORIES:
        q = q.filter_by(category=cat_filter)
    all_challenges = q.all()
    return render_template("challenges.html", challenges=all_challenges,
                           categories=CHALLENGE_CATEGORIES, cat_filter=cat_filter,
                           viewer=get_viewer(), today=date.today())


@app.route("/challenges/<int:cid>")
def challenge_detail(cid):
    challenge = Challenge.query.get_or_404(cid)
    viewer    = get_viewer()

    # Load all participant rows in one query
    part_rows = db.session.execute(
        challenge_participants.select().where(
            challenge_participants.c.challenge_id == cid
        )
    ).all()
    completed_ids = {r.user_id for r in part_rows if r.completed}
    proof_map     = {r.user_id: r.proof for r in part_rows if r.completed}

    participants = challenge.participants.all()
    completed    = [p for p in participants if p.id in completed_ids]

    viewer_joined    = viewer and any(r.user_id == viewer.id for r in part_rows)
    viewer_completed = viewer and viewer.id in completed_ids

    # Reaction counts and viewer reactions — 2 queries total
    rxn_rows = (db.session.query(ChallengeReaction.reaction, func.count())
                .filter_by(challenge_id=cid)
                .group_by(ChallengeReaction.reaction)
                .all())
    reaction_counts = dict(rxn_rows)
    viewer_reactions = set()
    if viewer:
        viewer_reactions = {
            r.reaction for r in
            ChallengeReaction.query.filter_by(challenge_id=cid, user_id=viewer.id).all()
        }

    # Like counts and viewer likes — 2 queries total
    lk_rows = (db.session.query(CompletionLike.completed_user_id, func.count())
               .filter_by(challenge_id=cid)
               .group_by(CompletionLike.completed_user_id)
               .all())
    like_counts = dict(lk_rows)
    viewer_liked = set()
    if viewer:
        viewer_liked = {
            r.completed_user_id for r in
            CompletionLike.query.filter_by(challenge_id=cid, liker_id=viewer.id).all()
        }

    return render_template("challenge_detail.html", challenge=challenge,
                           viewer=viewer, participants=participants, completed=completed,
                           completed_ids=completed_ids, proof_map=proof_map,
                           viewer_joined=viewer_joined, viewer_completed=viewer_completed,
                           reaction_counts=reaction_counts, viewer_reactions=viewer_reactions,
                           like_counts=like_counts, viewer_liked=viewer_liked,
                           today=date.today())


@app.route("/challenges/<int:cid>/join", methods=["POST"])
def challenge_join(cid):
    user = get_viewer()
    if not user:
        return redirect(url_for("login"))
    challenge = Challenge.query.get_or_404(cid)
    if not challenge.is_joined(user):
        challenge.participants.append(user)
        db.session.commit()
    return redirect(url_for("challenge_detail", cid=cid))


@app.route("/challenges/<int:cid>/leave", methods=["POST"])
def challenge_leave(cid):
    user = get_viewer()
    if not user:
        return redirect(url_for("login"))
    challenge = Challenge.query.get_or_404(cid)
    if challenge.is_joined(user) and not challenge.is_completed_by(user):
        challenge.participants.remove(user)
        db.session.commit()
    return redirect(url_for("challenge_detail", cid=cid))


ALLOWED_PROOF = {"png", "jpg", "jpeg", "gif", "webp", "mp4", "mov", "webm"}

@app.route("/challenges/<int:cid>/complete", methods=["POST"])
def challenge_complete(cid):
    user = get_viewer()
    if not user:
        return redirect(url_for("login"))
    challenge = Challenge.query.get_or_404(cid)
    if not challenge.is_joined(user):
        return redirect(url_for("challenge_detail", cid=cid))
    file = request.files.get("proof")
    if not file or file.filename == "":
        flash("Please upload a photo or video as proof.", "warning")
        return redirect(url_for("challenge_detail", cid=cid))
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_PROOF:
        flash("Allowed formats: images (jpg, png, gif, webp) or videos (mp4, mov, webm).", "danger")
        return redirect(url_for("challenge_detail", cid=cid))
    filename = f"proof_{cid}_{user.id}.{ext}"
    file.save(os.path.join(app.config["UPLOAD_FOLDER"], filename))
    is_first_completion = not Achievement.query.filter_by(
        user_id=user.id, kind="first_completion").first()
    db.session.execute(
        challenge_participants.update()
        .where(
            (challenge_participants.c.challenge_id == cid) &
            (challenge_participants.c.user_id == user.id)
        )
        .values(completed=True, proof=filename)
    )
    if is_first_completion:
        grant_achievement(user, "first_completion")
    db.session.commit()
    return redirect(url_for("challenge_detail", cid=cid))



@app.route("/challenges/<int:cid>/like/<int:uid>", methods=["POST"])
def completion_like(cid, uid):
    viewer = get_viewer()
    if not viewer:
        return redirect(url_for("login"))
    existing = CompletionLike.query.filter_by(
        challenge_id=cid, completed_user_id=uid, liker_id=viewer.id).first()
    if existing:
        db.session.delete(existing)
    else:
        db.session.add(CompletionLike(
            challenge_id=cid, completed_user_id=uid, liker_id=viewer.id))
    db.session.commit()
    return redirect(url_for("challenge_detail", cid=cid))


@app.route("/challenges/<int:cid>/react", methods=["POST"])
def challenge_react(cid):
    user = get_viewer()
    if not user:
        return redirect(url_for("login"))
    kind = request.form.get("reaction", "")
    if kind not in ("up", "down", "heart"):
        return redirect(url_for("challenge_detail", cid=cid))
    existing = ChallengeReaction.query.filter_by(challenge_id=cid, user_id=user.id).first()
    if existing and existing.reaction == kind:
        db.session.delete(existing)
    else:
        if existing:
            db.session.delete(existing)
        db.session.add(ChallengeReaction(challenge_id=cid, user_id=user.id, reaction=kind))
    db.session.commit()
    return redirect(url_for("challenge_detail", cid=cid))


def get_admin_user():
    u = get_viewer()
    return u if u and u.is_admin else None


@app.route("/admin")
def admin_panel():
    if not get_admin_user():
        return redirect(url_for("challenges"))
    users         = User.query.order_by(User.id).all()
    challenges    = Challenge.query.order_by(Challenge.created_at.desc()).all()
    posts         = Post.query.order_by(Post.created_at.desc()).all()
    forum_topics  = ForumTopic.query.order_by(ForumTopic.created_at.desc()).all()
    forum_comments = ForumComment.query.order_by(ForumComment.created_at.desc()).all()
    stats = {
        "users":          User.query.count(),
        "challenges":     Challenge.query.count(),
        "posts":          Post.query.count(),
        "messages":       Message.query.count(),
        "reactions":      ChallengeReaction.query.count(),
        "forum_topics":   ForumTopic.query.count(),
        "forum_comments": ForumComment.query.count(),
    }
    return render_template("admin.html", users=users, challenges=challenges,
                           posts=posts, forum_topics=forum_topics,
                           forum_comments=forum_comments, stats=stats)


@app.route("/admin/delete/user/<int:uid>", methods=["POST"])
def admin_delete_user(uid):
    admin = get_admin_user()
    if not admin:
        return redirect(url_for("challenges"))
    user = User.query.get_or_404(uid)
    if user.id == admin.id:
        flash("You cannot delete your own account.", "danger")
        return redirect(url_for("admin_panel"))
    # Remove from association tables
    db.session.execute(challenge_participants.delete().where(
        challenge_participants.c.user_id == uid))
    db.session.execute(follows.delete().where(
        (follows.c.follower_id == uid) | (follows.c.followed_id == uid)))
    ChallengeReaction.query.filter_by(user_id=uid).delete()
    CompletionLike.query.filter(
        (CompletionLike.liker_id == uid) | (CompletionLike.completed_user_id == uid)).delete()
    Message.query.filter(
        (Message.sender_id == uid) | (Message.receiver_id == uid)).delete()
    Post.query.filter_by(user_id=uid).delete()
    ForumComment.query.filter_by(user_id=uid).delete()
    for t in ForumTopic.query.filter_by(user_id=uid).all():
        ForumComment.query.filter_by(topic_id=t.id).delete()
        db.session.delete(t)
    Challenge.query.filter_by(creator_id=uid).delete()
    db.session.delete(user)
    db.session.commit()
    flash(f"User #{uid} deleted.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/delete/challenge/<int:cid>", methods=["POST"])
def admin_delete_challenge(cid):
    if not get_admin_user():
        return redirect(url_for("challenges"))
    challenge = Challenge.query.get_or_404(cid)
    db.session.execute(challenge_participants.delete().where(
        challenge_participants.c.challenge_id == cid))
    ChallengeReaction.query.filter_by(challenge_id=cid).delete()
    CompletionLike.query.filter_by(challenge_id=cid).delete()
    db.session.delete(challenge)
    db.session.commit()
    flash(f"Challenge #{cid} deleted.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/post/<int:pid>/delete", methods=["POST"])
def delete_post(pid):
    viewer = get_viewer()
    if not viewer:
        return redirect(url_for("login"))
    post = Post.query.get_or_404(pid)
    if post.user_id != viewer.id:
        return redirect(url_for("feed"))
    if post.image:
        img_path = os.path.join(app.config["UPLOAD_FOLDER"], post.image)
        if os.path.exists(img_path):
            os.remove(img_path)
    db.session.delete(post)
    db.session.commit()
    next_page = request.form.get("next", url_for("feed"))
    return redirect(next_page)


@app.route("/admin/delete/post/<int:pid>", methods=["POST"])
def admin_delete_post(pid):
    if not get_admin_user():
        return redirect(url_for("challenges"))
    post = Post.query.get_or_404(pid)
    db.session.delete(post)
    db.session.commit()
    flash(f"Post #{pid} deleted.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/toggle-admin/<int:uid>", methods=["POST"])
def admin_toggle_admin(uid):
    admin = get_admin_user()
    if not admin:
        return redirect(url_for("challenges"))
    user = User.query.get_or_404(uid)
    if user.id == admin.id:
        flash("You cannot change your own admin status.", "danger")
        return redirect(url_for("admin_panel"))
    user.is_admin = not user.is_admin
    db.session.commit()
    flash(f"{'Granted' if user.is_admin else 'Revoked'} admin for {user.username}.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/delete/forum-topic/<int:tid>", methods=["POST"])
def admin_delete_forum_topic(tid):
    if not get_admin_user():
        return redirect(url_for("challenges"))
    topic = ForumTopic.query.get_or_404(tid)
    db.session.delete(topic)
    db.session.commit()
    flash(f"Topic #{tid} deleted.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/delete/forum-comment/<int:cid>", methods=["POST"])
def admin_delete_forum_comment(cid):
    if not get_admin_user():
        return redirect(url_for("challenges"))
    comment = ForumComment.query.get_or_404(cid)
    db.session.delete(comment)
    db.session.commit()
    flash(f"Comment #{cid} deleted.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/login/2fa", methods=["GET", "POST"])
def login_2fa():
    username = session.get("2fa_pending")
    if not username:
        return redirect(url_for("login"))
    if request.method == "POST":
        code = request.form.get("code", "").strip()
        user = User.query.filter_by(username=username).first()
        if user and user.totp_secret and pyotp.TOTP(user.totp_secret).verify(code, valid_window=1):
            session.pop("2fa_pending", None)
            session["username"] = username
            return redirect(url_for("user_page"))
        flash("Invalid code. Please try again.", "danger")
    return render_template("2fa_verify.html")


@app.route("/settings/banner-color", methods=["POST"])
def settings_banner_color():
    user = get_viewer()
    if not user:
        return redirect(url_for("login"))
    color = request.form.get("color", "").strip()
    if color and len(color) == 7 and color.startswith("#"):
        user.banner_color = color
        db.session.commit()
    return redirect(url_for("user_page"))


@app.route("/settings/password", methods=["POST"])
def settings_password():
    user = get_viewer()
    if not user:
        return redirect(url_for("login"))
    current = request.form.get("current_password", "")
    new_pw  = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")
    if not user.check_password(current):
        flash("Current password is incorrect.", "danger")
    elif len(new_pw) < 6:
        flash("New password must be at least 6 characters.", "warning")
    elif new_pw != confirm:
        flash("New passwords do not match.", "danger")
    else:
        user.set_password(new_pw)
        db.session.commit()
        flash("Password updated successfully.", "success")
    return redirect(url_for("user_page"))


@app.route("/settings/2fa/setup", methods=["GET", "POST"])
def settings_2fa_setup():
    user = get_viewer()
    if not user:
        return redirect(url_for("login"))
    me = user.username
    if request.method == "POST":
        code   = request.form.get("code", "").strip()
        secret = session.get("2fa_secret")
        if secret and pyotp.TOTP(secret).verify(code, valid_window=1):
            user.totp_secret  = secret
            user.totp_enabled = True
            db.session.commit()
            session.pop("2fa_secret", None)
            flash("Two-factor authentication enabled.", "success")
            return redirect(url_for("user_page"))
        flash("Invalid code. Please try again.", "danger")
        return redirect(url_for("settings_2fa_setup"))
    secret = pyotp.random_base32()
    session["2fa_secret"] = secret
    qr_b64 = make_qr_b64(secret, me)
    return render_template("2fa_setup.html", secret=secret, qr_b64=qr_b64)


@app.route("/settings/2fa/disable", methods=["POST"])
def settings_2fa_disable():
    user = get_viewer()
    if not user:
        return redirect(url_for("login"))
    if not user.check_password(request.form.get("password", "")):
        flash("Incorrect password.", "danger")
    else:
        user.totp_enabled = False
        user.totp_secret  = None
        db.session.commit()
        flash("Two-factor authentication disabled.", "success")
    return redirect(url_for("user_page"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/forum", methods=["GET", "POST"])
def forum():
    if request.method == "POST":
        viewer = get_viewer()
        if not viewer:
            return redirect(url_for("login"))
        title = request.form.get("title", "").strip()
        body  = request.form.get("body",  "").strip()
        if title and body:
            db.session.add(ForumTopic(title=title, body=body, user_id=viewer.id))
            db.session.commit()
        return redirect(url_for("forum"))
    topics = (ForumTopic.query
              .order_by(ForumTopic.created_at.desc())
              .all())
    comment_counts = {
        t.id: t.comments.count() for t in topics
    }
    return render_template("forum.html", topics=topics,
                           comment_counts=comment_counts, viewer=get_viewer())


@app.route("/forum/<int:tid>", methods=["GET", "POST"])
def forum_topic(tid):
    topic = ForumTopic.query.get_or_404(tid)
    if request.method == "POST":
        viewer = get_viewer()
        if not viewer:
            return redirect(url_for("login"))
        body = request.form.get("body", "").strip()
        if body:
            db.session.add(ForumComment(body=body, user_id=viewer.id, topic_id=tid))
            db.session.commit()
        return redirect(url_for("forum_topic", tid=tid))
    comments = (ForumComment.query
                .filter_by(topic_id=tid)
                .order_by(ForumComment.created_at.asc())
                .all())
    return render_template("forum_topic.html", topic=topic,
                           comments=comments, viewer=get_viewer())


if __name__ == "__main__":
    app.run(debug=True, port=8080)
