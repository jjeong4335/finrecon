import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "webapp.settings")
application = get_wsgi_application()

if os.environ.get("FINRECON_WARM_CACHE", "1") == "1":
    import threading

    from webapp.views import warm_retail_cache

    threading.Thread(target=warm_retail_cache, daemon=True).start()
