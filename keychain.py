"""Small macOS Keychain adapter; secrets never appear in process arguments."""

import ctypes as C
import sys


class KeychainError(Exception):
    pass


class Keychain:
    def __init__(self, service="com.schwark.aqara", account="api-key"):
        self.service = service
        self.account = account

    def _call(self, operation, secret=None):
        if sys.platform != "darwin":
            raise KeychainError("macOS Keychain is only available on macOS.")
        cf = C.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        sec = C.CDLL("/System/Library/Frameworks/Security.framework/Security")
        ptr = C.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ptr, C.c_char_p, C.c_uint32]
        cf.CFStringCreateWithCString.restype = ptr
        cf.CFDictionaryCreateMutable.argtypes = [ptr, C.c_long, ptr, ptr]
        cf.CFDictionaryCreateMutable.restype = ptr
        cf.CFDictionarySetValue.argtypes = [ptr, ptr, ptr]
        cf.CFDataCreate.argtypes = [ptr, C.c_char_p, C.c_long]
        cf.CFDataCreate.restype = ptr
        cf.CFDataGetLength.argtypes = [ptr]
        cf.CFDataGetLength.restype = C.c_long
        cf.CFDataGetBytePtr.argtypes = [ptr]
        cf.CFDataGetBytePtr.restype = ptr
        cf.CFRelease.argtypes = [ptr]
        for name, args in (("SecItemCopyMatching", [ptr, C.POINTER(ptr)]),
                           ("SecItemAdd", [ptr, ptr]),
                           ("SecItemUpdate", [ptr, ptr]),
                           ("SecItemDelete", [ptr])):
            getattr(sec, name).argtypes = args
            getattr(sec, name).restype = C.c_int32
        refs = []

        def constant(name):
            return ptr.in_dll(sec, name).value

        def string(value):
            ref = cf.CFStringCreateWithCString(None, value.encode("utf-8"), 0x08000100)
            refs.append(ref)
            return ref

        def dictionary(entries):
            ref = cf.CFDictionaryCreateMutable(None, 0, None, None)
            refs.append(ref)
            for key, value in entries:
                cf.CFDictionarySetValue(ref, constant(key), value)
            return ref

        try:
            query = dictionary([
                ("kSecClass", constant("kSecClassGenericPassword")),
                ("kSecAttrService", string(self.service)),
                ("kSecAttrAccount", string(self.account)),
            ])
            if operation == "get":
                cf.CFDictionarySetValue(query, constant("kSecReturnData"),
                                        ptr.in_dll(cf, "kCFBooleanTrue").value)
                result = ptr()
                status = sec.SecItemCopyMatching(query, C.byref(result))
                if status == -25300:  # errSecItemNotFound
                    return None
                if status == 0:
                    try:
                        return C.string_at(cf.CFDataGetBytePtr(result),
                                           cf.CFDataGetLength(result)).decode("utf-8")
                    finally:
                        cf.CFRelease(result)
            elif operation == "delete":
                status = sec.SecItemDelete(query)
                if status == -25300:
                    return None
            else:
                encoded = secret.encode("utf-8")
                data = cf.CFDataCreate(None, encoded, len(encoded))
                refs.append(data)
                changes = dictionary([("kSecValueData", data)])
                status = sec.SecItemUpdate(query, changes)
                if status == -25300:
                    cf.CFDictionarySetValue(query, constant("kSecValueData"), data)
                    status = sec.SecItemAdd(query, None)
            if status != 0:
                raise KeychainError("Keychain access failed (%s). Unlock your login keychain and try again." % status)
        finally:
            for ref in reversed(refs):
                cf.CFRelease(ref)

    def get(self):
        return self._call("get")

    def set(self, secret):
        if not secret or not secret.strip():
            raise KeychainError("The API key cannot be empty.")
        self._call("set", secret.strip())

    def delete(self):
        self._call("delete")
