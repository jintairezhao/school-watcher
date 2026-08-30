"""路由蓝图注册"""
from backend.routes import (account, admin, auth, pages, schools, scrape,
                            discovery, settings, search, subscriptions)


def register_blueprints(app):
    app.register_blueprint(auth.bp)
    app.register_blueprint(pages.bp)
    app.register_blueprint(schools.bp)
    app.register_blueprint(scrape.bp)
    app.register_blueprint(discovery.bp)
    app.register_blueprint(settings.bp)
    app.register_blueprint(search.bp)
    app.register_blueprint(subscriptions.bp)
    app.register_blueprint(account.bp)
    app.register_blueprint(admin.bp)
