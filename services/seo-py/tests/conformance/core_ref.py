"""The conformance suite's OWN copy of the closed label lists of core-spec/1 section 1 (L-1..L-8), written out as
escapes, independent of ``agents.secret_guard``. ``test_core_labels_closed.py`` asserts the module's lists equal this
copy; every generator of the suite reads its entries from here, never from the module.

Non-Latin label words are never printed: an entry is named by its id (``J00``..``J43``, ``P00``..``P26``,
``S00``..``S22``, ``F1``, ``F2``, ``H1``..``H5``) and an English gloss."""

# L-2: the long labels, read with every non-letter removed (44)
JOINED = (
    "password", "passwd", "passcode", "passphrase", "apikey", "accesskey", "accesstoken", "authtoken",
    "secretkey", "privatekey", "clientsecret", "sessionid", "accesscode", "securitycode", "sessiontoken", "seedphrase",
    "recoveryphrase", "mnemonic", "credential", "connectionstring", "accountkey", "refreshtoken", "bearertoken",
    "sharedaccesssignature", "contrasena", "motdepasse", "passwort", "kennwort", "senha", "wachtwoord", "losenord",
    "adgangskode", "salasana", "haslo", "katasandi", "matkhau", "parolasi", "heslo", "jelszo", "lozinka",
    "paroladordine", "palavrapasse", "contrasenya", "phrasedepasse",
)
# L-3: the short labels, read as a whole piece (27), sorted
PIECES = (
    "apikeys", "auth", "authorisation", "authorization", "bearer", "chiave", "clave", "cle", "cookie", "cookies",
    "credentials", "otp", "parola", "pass", "passwords", "pin", "pw", "pwd", "schlussel", "secret", "secrets", "sid",
    "sifre", "sig", "token", "tokens", "totp",
)
# L-4: the script labels (23), by index S00..S22: (entry, script, gloss)
SCRIPT = (
    ("\u043f\u0430\u0440\u043e\u043b", "CYRILLIC", "password (ru / uk, stem)"),
    ("\u043a\u043b\u044e\u0447 api", "CYRILLIC", "API key (ru 'key api')"),
    ("\u5bc6\u7801", "CJK", "password (simplified)"),
    ("\u5bc6\u78bc", "CJK", "password (traditional)"),
    ("\u53e3\u4ee4", "CJK", "password ('mouth order')"),
    ("\u5bc6\u94a5", "CJK", "secret key (simplified)"),
    ("\u79d8\u94a5", "CJK", "secret key (simplified, alternative)"),
    ("\u91d1\u9470", "CJK", "secret key (traditional)"),
    ("\u4ee4\u724c", "CJK", "token"),
    ("\u30d1\u30b9\u30ef\u30fc\u30c9", "KATAKANA", "password (ja)"),
    ("\u6697\u8a3c\u756a\u53f7", "CJK", "PIN (ja)"),
    ("\u30c8\u30fc\u30af\u30f3", "KATAKANA", "token (ja)"),
    ("\ube44\ubc00\ubc88\ud638", "HANGUL", "password (ko, 'secret number')"),
    ("\ube44\ubc88", "HANGUL", "password (ko, abbreviation)"),
    ("\uc554\ud638", "HANGUL", "password / cipher (ko)"),
    ("\ud1a0\ud070", "HANGUL", "token (ko)"),
    ("\u0643\u0644\u0645\u0629 \u0627\u0644\u0645\u0631\u0648\u0631", "ARABIC", "password (ar, 'word of passing')"),
    ("\u0643\u0644\u0645\u0629 \u0627\u0644\u0633\u0631", "ARABIC", "password (ar, 'secret word')"),
    ("\u0631\u0645\u0632 \u0639\u0628\u0648\u0631", "ARABIC", "password (fa)"),
    ("\u092a\u093e\u0938\u0935\u0930\u094d\u0921", "DEVANAGARI", "password (hi)"),
    ("\u0e23\u0e2b\u0e31\u0e2a\u0e1c\u0e48\u0e32\u0e19", "THAI", "password (th)"),
    ("\u05e1\u05d9\u05e1\u05de\u05d4", "HEBREW", "password (he)"),
    ("\u03ba\u03c9\u03b4\u03b9\u03ba\u03bf\u03c2 \u03c0\u03c1\u03bf\u03c3\u03b2\u03b1\u03c3", "GREEK",
     "password (el, 'code' + stem of 'access')"),
)
# L-5: the full forms both lanes must match whole at import (2)
CV_SCRIPT_FULL = (
    "\u043f\u0430\u0440\u043e\u043b\u044c",
    "\u03ba\u03c9\u03b4\u03b9\u03ba\u03cc\u03c2 \u03c0\u03c1\u03cc\u03c3\u03b2\u03b1\u03c3\u03b7\u03c2",
)
# L-6: the historical forms (entries of earlier releases' own lists, glued)
HISTORY = (
    ("H1", "\u0643\u0644\u0645\u0629\u0627\u0644\u0645\u0631\u0648\u0631", "S16 glued (ffd28ca8 / cbdf5e78 entry)"),
    ("H2", "\u0643\u0644\u0645\u0629\u0627\u0644\u0633\u0631", "S17 glued"),
    ("H3", "\u0631\u0645\u0632\u0639\u0628\u0648\u0631", "S18 glued"),
    ("H4", "\u03ba\u03c9\u03b4\u03b9\u03ba\u03bf\u03c3\u03c0\u03c1\u03bf\u03c3\u03b2", "the Greek stem ffd28ca8 listed"),
    ("H5", "\u03ba\u03c9\u03b4\u03b9\u03ba\u03bf\u03c3\u03c0\u03c1\u03bf\u03c3\u03b2\u03b1\u03c3",
     "S22 unaccented glued (cbdf5e78 entry)"),
)

# L-7: the declared closed sub-lists
RU_ENDINGS = ("", "\u044c", "\u044f", "\u044e", "\u0435\u043c", "\u0435", "\u0438", "\u0435\u0439", "\u044f\u043c",
              "\u044f\u043c\u0438", "\u044f\u0445", "\u0456", "\u0456\u0432")
KO_PARTICLES = ("", "\uc774", "\uac00", "\uc740", "\ub294", "\uc744", "\ub97c", "\uc758", "\uc5d0", "\uc5d0\uc11c", "\ub85c",
                "\uc73c\ub85c", "\uc640", "\uacfc", "\ub3c4", "\ub9cc", "\uae4c\uc9c0", "\ubd80\ud130", "\uc774\ub098", "\ub098",
                "\ub791", "\uc774\ub791", "\ud558\uace0", "\uc5d0\uac8c", "\uc785\ub2c8\ub2e4", "\uc774\ub2e4", "\uc608\uc694",
                "\uc774\uc5d0\uc694", "\ub4e4", "\ub4e4\uc744", "\ub4e4\uc774", "\ub4e4\uc740", "\ub4e4\uc758")
EL_ENDINGS = ("", "\u03b7", "\u03b7\u03c2", "\u03b5\u03c9\u03c2", "\u03b5\u03b9\u03c2", "\u03b5\u03c9\u03bd")
HI_ENDINGS = ("", "\u094b\u0902", "\u094d\u0938")
HE_PROCLITICS = ("\u05d5\u05d4\u05d1\u05dc\u05de\u05e9\u05db", 3)     # and, the, in, to, from, that, as
AR_PROCLITICS = ("\u0648\u0641\u0628\u0644\u0643", 2)                 # and, so, with, to, as
# the Greek written spellings (5 accented case forms + H4), as the entry declares them
EL_WRITTEN = (
    "\u03ba\u03c9\u03b4\u03b9\u03ba\u03cc\u03c2 \u03c0\u03c1\u03cc\u03c3\u03b2\u03b1\u03c3\u03b7",
    "\u03ba\u03c9\u03b4\u03b9\u03ba\u03cc\u03c2 \u03c0\u03c1\u03cc\u03c3\u03b2\u03b1\u03c3\u03b7\u03c2",
    "\u03ba\u03c9\u03b4\u03b9\u03ba\u03cc\u03c2 \u03c0\u03c1\u03bf\u03c3\u03b2\u03ac\u03c3\u03b5\u03c9\u03c2",
    "\u03ba\u03c9\u03b4\u03b9\u03ba\u03cc\u03c2 \u03c0\u03c1\u03bf\u03c3\u03b2\u03ac\u03c3\u03b5\u03b9\u03c2",
    "\u03ba\u03c9\u03b4\u03b9\u03ba\u03cc\u03c2 \u03c0\u03c1\u03bf\u03c3\u03b2\u03ac\u03c3\u03b5\u03c9\u03bd",
    "\u03ba\u03c9\u03b4\u03b9\u03ba\u03bf\u03c2 \u03c0\u03c1\u03bf\u03c3\u03b2",
)
KO_APART = "\ube44\ubc00 \ubc88\ud638"                                  # S12's two words apart
# L-8: not entries
NOT_ENTRIES = ("key", "p4ssw0rd", "s3cret", "t0ken", "pa$$word")


def entries() -> list:
    """The matrix entry set E (L-6): (id, entry, kind, script, gloss); 44 + 27 + 23 + 2 + 5 = 101."""
    out = [(f"J{i:02d}", e, "joined", "LATIN", e) for i, e in enumerate(JOINED)]
    out += [(f"P{i:02d}", e, "piece", "LATIN", e) for i, e in enumerate(PIECES)]
    out += [(f"S{i:02d}", e, "script", sc, g) for i, (e, sc, g) in enumerate(SCRIPT)]
    out += [("F1", CV_SCRIPT_FULL[0], "full", "CYRILLIC", "password (ru nominative)"),
            ("F2", CV_SCRIPT_FULL[1], "full", "GREEK", "password (el full nominative)")]
    out += [(h, e, "history", "GREEK" if h in ("H4", "H5") else "ARABIC", g) for h, e, g in HISTORY]
    return out


ENTRY_COUNT = 101

# the natural spellings of the Latin entries (as their languages write them): the "as written" bases of the matrix
NATURAL = {
    "contrasena": ("contrase\u00f1a",), "motdepasse": ("mot de passe",), "losenord": ("l\u00f6senord",),
    "haslo": ("has\u0142o",), "katasandi": ("kata sandi",), "matkhau": ("m\u1eadt kh\u1ea9u",),
    "parolasi": ("parolas\u0131",), "jelszo": ("jelsz\u00f3",),
    "paroladordine": ("parola d'ordine", "parola d\u2019ordine"), "palavrapasse": ("palavra-passe",),
    "phrasedepasse": ("phrase de passe",), "sifre": ("\u015fifre",), "schlussel": ("schl\u00fcssel",),
    "cle": ("cl\u00e9",), "apikey": ("api key", "api_key"), "accesskey": ("access key",),
    "accesstoken": ("access token",), "authtoken": ("auth token",), "secretkey": ("secret key",),
    "privatekey": ("private key",), "clientsecret": ("client secret",), "sessionid": ("session id",),
    "accesscode": ("access code",), "securitycode": ("security code",), "sessiontoken": ("session token",),
    "seedphrase": ("seed phrase",), "recoveryphrase": ("recovery phrase",),
    "connectionstring": ("connection string",), "accountkey": ("account key",), "refreshtoken": ("refresh token",),
    "bearertoken": ("bearer token",), "sharedaccesssignature": ("shared access signature",),
}
# the Latin entries whose natural spelling carries a diacritic (SP-11: written without it is the listed form)
LATIN_MARKED = tuple(e for e, ns in NATURAL.items() if any(not n.isascii() for n in ns))
