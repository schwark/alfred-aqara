"""Select the workflow variant without changing the stable Agent default."""
import os
from common import Store


def get_store():
    if os.environ.get("AQARA_BACKEND") == "mobile":
        from mobile_backend import MobileStore
        return MobileStore()
    return Store()
