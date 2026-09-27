"""路由蓝图注册"""
from backend.routes import (account, admin, auth, pages, schools, scrape,
                            discovery, settings, search, subscriptions, library, source_structure, content, operations, browser_sessions, browser_origin, summaries, ai_settings, source_governance)


def register_blueprints(app):
    if app.config.get('DESKTOP_MODE'):
        from backend.routes.desktop import bp
        app.register_blueprint(bp)
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
    app.register_blueprint(library.bp)
    app.register_blueprint(source_structure.bp)

    app.register_blueprint(content.bp)
    app.register_blueprint(operations.bp)
    app.register_blueprint(browser_sessions.bp)
    app.register_blueprint(browser_origin.bp)

    app.register_blueprint(summaries.bp)
    app.register_blueprint(ai_settings.bp)
    app.register_blueprint(source_governance.bp)
