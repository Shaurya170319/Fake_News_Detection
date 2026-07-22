
from flask import Flask
from flask_login import LoginManager, current_user
from flask_bootstrap import Bootstrap
from config import Config
from app.models import db

login_manager = LoginManager()
login_manager.login_view = 'main.login'

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)
    login_manager.init_app(app)
    Bootstrap(app)

    from app.routes import bp as main_bp
    app.register_blueprint(main_bp)

    # Make current_user available in all templates
    @app.context_processor
    def inject_user():
        return dict(current_user=current_user)

    with app.app_context():
        db.create_all()

    return app

@login_manager.user_loader
def load_user(user_id):
    from app.models import User
    return User.query.get(int(user_id))
