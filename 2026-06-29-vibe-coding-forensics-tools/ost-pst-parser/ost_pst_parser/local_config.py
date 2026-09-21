"""Organization-local tuning for Outlook evidence triage.

Edit this file when adapting the viewer for another company. The rest of the
code imports these values rather than hardcoding CGI-specific domains or MSIP
label identifiers.
"""

# Email domains treated as internal/corporate. Subdomains are also considered
# internal, so "emea.cgi.com" is internal when "cgi.com" is listed here.
INTERNAL_EMAIL_DOMAINS = {
    "cgi.com",
    "logica.com",
}

# Domains or exact addresses ignored by external/personal highlighting. Use this
# for trusted service accounts, mailing-list systems, partner gateways, etc.
IGNORED_EMAIL_DOMAINS = {
    "mail.microsoft", # noisy teams notifications
    "groupecgi.onmicrosoft.com", # same from ms365/teams
}

IGNORED_EMAIL_ADDRESSES = {
    "noreply@eu.yammer.com",
}

# Consumer/personal mailbox providers highlighted in address columns.
PERSONAL_EMAIL_DOMAINS = {
    "aol.com",
    "alice.it",
    "aliceadsl.fr",
    "ameritech.net",
    "att.net",
    "bellsouth.net",
    "bk.ru",
    "bluewin.ch",
    "bol.com.br",
    "bt.com",
    "btinternet.com",
    "charter.net",
    "chello.nl",
    "club-internet.fr",
    "comcast.net",
    "cox.net",
    "earthlink.net",
    "email.com",
    "email.cz",
    "embarqmail.com",
    "fastmail.com",
    "free.fr",
    "freenet.de",
    "gmail.com",
    "gmx.com",
    "gmx.de",
    "gmx.fr",
    "gmx.net",
    "googlemail.com",
    "hanmail.net",
    "hey.com",
    "home.nl",
    "hotmail.ca",
    "hotmail.co.in",
    "hotmail.co.uk",
    "hotmail.com",
    "hotmail.com.au",
    "hotmail.de",
    "hotmail.fr",
    "hotmail.it",
    "hushmail.com",
    "icloud.com",
    "iinet.net.au",
    "inbox.ru",
    "juno.com",
    "laposte.net",
    "libero.it",
    "list.ru",
    "live.co.uk",
    "live.com",
    "live.com.au",
    "live.fr",
    "live.in",
    "live.se",
    "lycos.com",
    "mac.com",
    "mail.com",
    "mail.ru",
    "mailbox.org",
    "me.com",
    "msn.com",
    "neuf.fr",
    "ntlworld.com",
    "numericable.fr",
    "o2.co.uk",
    "online.de",
    "optusnet.com.au",
    "orange.fr",
    "outlook.com",
    "outlook.fr",
    "outlook.in",
    "pacbell.net",
    "pm.me",
    "proton.me",
    "protonmail.com",
    "qq.com",
    "rambler.ru",
    "rediffmail.com",
    "rocketmail.com",
    "rogers.com",
    "sbcglobal.net",
    "sfr.fr",
    "shaw.ca",
    "skynet.be",
    "sky.com",
    "snet.net",
    "spectrum.net",
    "sympatico.ca",
    "talktalk.net",
    "telenet.be",
    "telia.com",
    "telia.se",
    "telus.net",
    "tiscali.co.uk",
    "tiscali.it",
    "t-online.de",
    "tutanota.com",
    "tutamail.com",
    "verizon.net",
    "virgin.net",
    "virginmedia.com",
    "wanadoo.fr",
    "web.de",
    "windstream.net",
    "wp.pl",
    "xtra.co.nz",
    "yahoo.ca",
    "yahoo.co.in",
    "yahoo.co.uk",
    "yahoo.com",
    "yahoo.com.au",
    "yahoo.de",
    "yahoo.fr",
    "yahoo.in",
    "yandex.com",
    "yandex.ru",
    "ymail.com",
    "zoho.com",
}

# Microsoft Purview / MIP / MSIP label UUIDs seen in message properties.
# Values are (display name, enabled, display color).
MSIP_CLASSIFICATION_LABELS = {
    "d9290083-bd2f-48a2-8ac5-09a524b17d15": ("Public", True, "#317100"),
    "7522efd8-5e7d-4846-a1c9-63a2475d3257": ("Internal", True, "#FF8C00"),
    "84bf0462-fe2b-4337-b997-42579ffc1583": ("TestLabel", False, "#ff8c00"),
    "586109c7-167f-47c4-85bd-eadbd4de44e1": ("T-Public", False, "#317100"),
    "26b08594-efe0-4de2-893f-3f2296699d3c": ("T-Internal", False, "#FF8C00"),
}

# MAPI property IDs that commonly carry MSIP label metadata.
MSIP_PROPERTY_IDS = {0x83D4, 0x8156, 0x83E3}
