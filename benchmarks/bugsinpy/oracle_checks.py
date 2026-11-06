"""Hand-written oracle assertions, one per benchmark bug.

Used only to validate the benchmark itself (the bug must manifest on the buggy tree
and disappear on the fixed tree under this machine's Python). Never shown to the agent.
"""
ORACLES = {
    "youtube-dl_3": "from youtube_dl.utils import unescapeHTML\nassert unescapeHTML('&a&quot;') == '&a\"'",
    "youtube-dl_11": "from youtube_dl.utils import str_to_int\nassert str_to_int(523) == 523",
    "youtube-dl_13": "from youtube_dl.utils import urljoin\nassert urljoin(None, 'rtmp://foo.de') == 'rtmp://foo.de'",
    "youtube-dl_17": "from youtube_dl.utils import cli_bool_option\nassert cli_bool_option({}, '--check-certificate', 'nocheckcertificate') == []",
    "youtube-dl_27": "from youtube_dl.utils import parse_dfxp_time_expr\nassert parse_dfxp_time_expr('00:00:01:100') == 1.1",
    "youtube-dl_29": "from youtube_dl.utils import unified_strdate\nassert unified_strdate('UNKNOWN DATE FORMAT') is None",
    "youtube-dl_43": "from youtube_dl.utils import url_basename\nassert url_basename('http://media.w3.org/2010/05/sintel/trailer.mp4') == 'trailer.mp4'",
    "thefuck_18": "from thefuck.rules.sudo import match\nfrom tests.utils import Command\nassert not match(Command(script='sudo ls', stderr='Permission denied'), None)",
    "thefuck_25": "from thefuck.rules.mkdir_p import get_new_command\nfrom tests.utils import Command\nassert get_new_command(Command(script='hdfs dfs -mkdir foo/bar/baz'), None) == 'hdfs dfs -mkdir -p foo/bar/baz'",
    "thefuck_27": "from thefuck.rules.open import get_new_command\nfrom tests.utils import Command\nassert get_new_command(Command(script='xdg-open foo.com'), None) == 'xdg-open http://foo.com'",
    "thefuck_31": "from thefuck.rules.git_diff_staged import get_new_command\nfrom tests.utils import Command\nassert get_new_command(Command(script='git diff foo'), None) == 'git diff --staged foo'",
    "thefuck_32": "from thefuck.rules.ls_lah import match\nfrom tests.utils import Command\nassert not match(Command(script='pacman -S binutils'), None)",
    "tqdm_1": "from tqdm.contrib import tenumerate\nassert list(tenumerate(['a', 'b'], start=10)) == [(10, 'a'), (11, 'b')]",
}
