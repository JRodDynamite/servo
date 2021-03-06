# Copyright 2013 The Servo Project Developers. See the COPYRIGHT
# file at the top-level directory of this distribution.
#
# Licensed under the Apache License, Version 2.0 <LICENSE-APACHE or
# http://www.apache.org/licenses/LICENSE-2.0> or the MIT license
# <LICENSE-MIT or http://opensource.org/licenses/MIT>, at your
# option. This file may not be copied, modified, or distributed
# except according to those terms.

import contextlib
import fnmatch
import itertools
import json
import os
import re
import site
import StringIO
import subprocess
import sys
from licenseck import MPL, APACHE, COPYRIGHT, licenses_toml, licenses_dep_toml
import colorama
import toml

CONFIG_FILE_PATH = os.path.join(".", "servo-tidy.toml")

# Default configs
config = {
    "skip-check-length": False,
    "skip-check-licenses": False,
    "ignore": {
        "files": [
            "./.",   # ignore hidden files
        ],
        "directories": [
            "./.",   # ignore hidden directories
        ],
        "packages": [],
    },
    "check_ext": {}
}

COMMENTS = ["// ", "# ", " *", "/* "]

# File patterns to include in the non-WPT tidy check.
FILE_PATTERNS_TO_CHECK = ["*.rs", "*.rc", "*.cpp", "*.c",
                          "*.h", "Cargo.lock", "*.py", "*.sh",
                          "*.toml", "*.webidl", "*.json", "*.html"]

# File patterns that are ignored for all tidy and lint checks.
FILE_PATTERNS_TO_IGNORE = ["*.#*", "*.pyc"]

SPEC_BASE_PATH = "components/script/dom/"

WEBIDL_STANDARDS = [
    "//www.khronos.org/registry/webgl/specs",
    "//developer.mozilla.org/en-US/docs/Web/API",
    "//dev.w3.org/2006/webapi",
    "//dev.w3.org/csswg",
    "//dev.w3.org/fxtf",
    "//dvcs.w3.org/hg",
    "//dom.spec.whatwg.org",
    "//domparsing.spec.whatwg.org",
    "//drafts.csswg.org/cssom",
    "//drafts.fxtf.org",
    "//encoding.spec.whatwg.org",
    "//fetch.spec.whatwg.org",
    "//html.spec.whatwg.org",
    "//url.spec.whatwg.org",
    "//xhr.spec.whatwg.org",
    "//w3c.github.io",
    "//heycam.github.io/webidl",
    "//webbluetoothcg.github.io/web-bluetooth/",
    "//svgwg.org/svg2-draft",
    # Not a URL
    "// This interface is entirely internal to Servo, and should not be" +
    " accessible to\n// web pages."
]


def is_iter_empty(iterator):
    try:
        obj = iterator.next()
        return True, itertools.chain((obj,), iterator)
    except StopIteration:
        return False, iterator


# A simple wrapper for iterators to show progress (note that it's inefficient for giant iterators)
def progress_wrapper(iterator):
    list_of_stuff = list(iterator)
    total_files, progress = len(list_of_stuff), 0
    for idx, thing in enumerate(list_of_stuff):
        progress = int(float(idx + 1) / total_files * 100)
        sys.stdout.write('\r  Progress: %s%% (%d/%d)' % (progress, idx + 1, total_files))
        sys.stdout.flush()
        yield thing


def filter_file(file_name):
    if any(file_name.startswith(ignored_file) for ignored_file in config["ignore"]["files"]):
        return False
    base_name = os.path.basename(file_name)
    if any(fnmatch.fnmatch(base_name, pattern) for pattern in FILE_PATTERNS_TO_IGNORE):
        return False
    return True


def filter_files(start_dir, only_changed_files, progress):
    file_iter = get_file_list(start_dir, only_changed_files, config["ignore"]["directories"])
    (has_element, file_iter) = is_iter_empty(file_iter)
    if not has_element:
        raise StopIteration
    if progress:
        file_iter = progress_wrapper(file_iter)

    for file_name in file_iter:
        base_name = os.path.basename(file_name)
        if not any(fnmatch.fnmatch(base_name, pattern) for pattern in FILE_PATTERNS_TO_CHECK):
            continue
        if not filter_file(file_name):
            continue
        yield file_name


def uncomment(line):
    for c in COMMENTS:
        if line.startswith(c):
            if line.endswith("*/"):
                return line[len(c):(len(line) - 3)].strip()
            return line[len(c):].strip()


def is_apache_licensed(header):
    if APACHE in header:
        return any(c in header for c in COPYRIGHT)


def check_license(file_name, lines):
    if any(file_name.endswith(ext) for ext in (".toml", ".lock", ".json", ".html")) or \
       config["skip-check-licenses"]:
        raise StopIteration

    if lines[0].startswith("#!") and lines[1].strip():
        yield (1, "missing blank line after shebang")

    blank_lines = 0
    max_blank_lines = 2 if lines[0].startswith("#!") else 1
    license_block = []

    for l in lines:
        l = l.rstrip('\n')
        if not l.strip():
            blank_lines += 1
            if blank_lines >= max_blank_lines:
                break
            continue
        line = uncomment(l)
        if line is not None:
            license_block.append(line)

    header = " ".join(license_block)
    valid_license = MPL in header or is_apache_licensed(header)
    acknowledged_bad_license = "xfail-license" in header
    if not (valid_license or acknowledged_bad_license):
        yield (1, "incorrect license")


def check_modeline(file_name, lines):
    for idx, line in enumerate(lines[:5]):
        if re.search('^.*[ \t](vi:|vim:|ex:)[ \t]', line):
            yield (idx + 1, "vi modeline present")
        elif re.search('-\*-.*-\*-', line, re.IGNORECASE):
            yield (idx + 1, "emacs file variables present")


def check_length(file_name, idx, line):
    if any(file_name.endswith(ext) for ext in (".lock", ".json", ".html", ".toml")) or \
       config["skip-check-length"]:
        raise StopIteration

    # Prefer shorter lines when shell scripting.
    max_length = 80 if file_name.endswith(".sh") else 120
    if len(line.rstrip('\n')) > max_length:
        yield (idx + 1, "Line is longer than %d characters" % max_length)


def check_whatwg_specific_url(idx, line):
    match = re.search(r"https://html\.spec\.whatwg\.org/multipage/[\w-]+\.html#([\w\:-]+)", line)
    if match is not None:
        preferred_link = "https://html.spec.whatwg.org/multipage/#{}".format(match.group(1))
        yield (idx + 1, "link to WHATWG may break in the future, use this format instead: {}".format(preferred_link))


def check_whatwg_single_page_url(idx, line):
    match = re.search(r"https://html\.spec\.whatwg\.org/#([\w\:-]+)", line)
    if match is not None:
        preferred_link = "https://html.spec.whatwg.org/multipage/#{}".format(match.group(1))
        yield (idx + 1, "links to WHATWG single-page url, change to multi page: {}".format(preferred_link))


def check_whitespace(idx, line):
    if line[-1] == "\n":
        line = line[:-1]
    else:
        yield (idx + 1, "no newline at EOF")

    if line.endswith(" "):
        yield (idx + 1, "trailing whitespace")

    if "\t" in line:
        yield (idx + 1, "tab on line")

    if "\r" in line:
        yield (idx + 1, "CR on line")


def check_by_line(file_name, lines):
    for idx, line in enumerate(lines):
        errors = itertools.chain(
            check_length(file_name, idx, line),
            check_whitespace(idx, line),
            check_whatwg_specific_url(idx, line),
            check_whatwg_single_page_url(idx, line),
        )

        for error in errors:
            yield error


def check_flake8(file_name, contents):
    from flake8.main import check_code

    if not file_name.endswith(".py"):
        raise StopIteration

    @contextlib.contextmanager
    def stdout_redirect(where):
        sys.stdout = where
        try:
            yield where
        finally:
            sys.stdout = sys.__stdout__

    ignore = {
        "W291",  # trailing whitespace; the standard tidy process will enforce no trailing whitespace
        "E501",  # 80 character line length; the standard tidy process will enforce line length
    }

    output = StringIO.StringIO()
    with stdout_redirect(output):
        check_code(contents, ignore=ignore)
    for error in output.getvalue().splitlines():
        _, line_num, _, message = error.split(":", 3)
        yield line_num, message.strip()


def check_lock(file_name, contents):
    def find_reverse_dependencies(dependency, version, content):
        dependency_prefix = "{} {}".format(dependency, version)
        for package in itertools.chain([content["root"]], content["package"]):
            for dependency in package.get("dependencies", []):
                if dependency.startswith(dependency_prefix):
                    yield package["name"]

    if not file_name.endswith(".lock"):
        raise StopIteration

    # package names to be neglected (as named by cargo)
    exceptions = config["ignore"]["packages"]

    # toml.py has a bug(?) that we trip up in [metadata] sections;
    # see https://github.com/uiri/toml/issues/61
    # This should only affect a very few lines (that have embedded ?branch=...),
    # and most of them won't be in the repo
    try:
        content = toml.loads(contents)
    except:
        print "WARNING!"
        print "WARNING! toml parsing failed for Cargo.lock, but ignoring..."
        print "WARNING!"
        raise StopIteration

    packages = {}
    for package in content.get("package", []):
        packages.setdefault(package["name"], []).append(package["version"])

    for (name, versions) in packages.iteritems():
        if name in exceptions or len(versions) <= 1:
            continue

        highest = max(versions)
        for version in versions:
            if version != highest:
                reverse_dependencies = "\n".join(
                    "\t\t{}".format(n)
                    for n in find_reverse_dependencies(name, version, content)
                )
                substitutions = {
                    "package": name,
                    "old_version": version,
                    "new_version": highest,
                    "reverse_dependencies": reverse_dependencies
                }
                message = """
duplicate versions for package "{package}"
\t\033[93mfound dependency on version {old_version}\033[0m
\t\033[91mbut highest version is {new_version}\033[0m
\t\033[93mtry upgrading with\033[0m \033[96m./mach cargo-update -p {package}:{old_version}\033[0m
\tThe following packages depend on version {old_version}:
{reverse_dependencies}
""".format(**substitutions).strip()
                yield (1, message)


def check_toml(file_name, lines):
    if not file_name.endswith("Cargo.toml"):
        raise StopIteration
    ok_licensed = False
    for idx, line in enumerate(lines):
        if line.find("*") != -1:
            yield (idx + 1, "found asterisk instead of minimum version number")
        for license_line in licenses_toml:
            ok_licensed |= (license_line in line)
    if not ok_licensed:
        yield (0, ".toml file should contain a valid license.")


def check_shell(file_name, lines):
    if not file_name.endswith(".sh"):
        raise StopIteration

    shebang = "#!/usr/bin/env bash"
    required_options = {"set -o errexit", "set -o nounset", "set -o pipefail"}

    did_shebang_check = False

    if not lines:
        yield (0, 'script is an empty file')
        return

    if lines[0].rstrip() != shebang:
        yield (1, 'script does not have shebang "{}"'.format(shebang))

    for idx in range(1, len(lines)):
        stripped = lines[idx].rstrip()
        # Comments or blank lines are ignored. (Trailing whitespace is caught with a separate linter.)
        if lines[idx].startswith("#") or stripped == "":
            continue

        if not did_shebang_check:
            if stripped in required_options:
                required_options.remove(stripped)
            else:
                # The first non-comment, non-whitespace, non-option line is the first "real" line of the script.
                # The shebang, options, etc. must come before this.
                if required_options:
                    formatted = ['"{}"'.format(opt) for opt in required_options]
                    yield (idx + 1, "script is missing options {}".format(", ".join(formatted)))
                did_shebang_check = True

        if "`" in stripped:
            yield (idx + 1, "script should not use backticks for command substitution")

        if " [ " in stripped or stripped.startswith("[ "):
            yield (idx + 1, "script should use `[[` instead of `[` for conditional testing")

        for dollar in re.finditer('\$', stripped):
            next_idx = dollar.end()
            if next_idx < len(stripped):
                next_char = stripped[next_idx]
                if not (next_char == '{' or next_char == '('):
                    yield(idx + 1, "variable substitutions should use the full \"${VAR}\" form")


def check_rust(file_name, lines):
    if not file_name.endswith(".rs") or \
       file_name.endswith(".mako.rs") or \
       file_name.endswith(os.path.join("style", "build.rs")) or \
       file_name.endswith(os.path.join("geckolib", "build.rs")) or \
       file_name.endswith(os.path.join("unit", "style", "stylesheets.rs")):
        raise StopIteration

    comment_depth = 0
    merged_lines = ''
    import_block = False
    whitespace = False

    prev_use = None
    prev_open_brace = False
    current_indent = 0
    prev_crate = {}
    prev_mod = {}

    decl_message = "{} is not in alphabetical order"
    decl_expected = "\n\t\033[93mexpected: {}\033[0m"
    decl_found = "\n\t\033[91mfound: {}\033[0m"

    for idx, original_line in enumerate(lines):
        # simplify the analysis
        line = original_line.strip()
        is_attribute = re.search(r"#\[.*\]", line)
        is_comment = re.search(r"^//|^/\*|^\*", line)

        # Simple heuristic to avoid common case of no comments.
        if '/' in line:
            comment_depth += line.count('/*')
            comment_depth -= line.count('*/')

        if line.endswith('\\'):
            merged_lines += line[:-1]
            continue
        if comment_depth:
            merged_lines += line
            continue
        if merged_lines:
            line = merged_lines + line
            merged_lines = ''

        # Ignore attributes, comments, and imports
        # Keep track of whitespace to enable checking for a merged import block
        if import_block:
            if not (is_comment or is_attribute or line.startswith("use ")):
                whitespace = line == ""

                if not whitespace:
                    import_block = False

        # get rid of strings and chars because cases like regex expression, keep attributes
        if not is_attribute:
            line = re.sub(r'"(\\.|[^\\"])*?"', '""', line)
            line = re.sub(r"'(\\.|[^\\'])*?'", "''", line)

        # get rid of comments
        line = re.sub('//.*?$|/\*.*?$|^\*.*?$', '//', line)

        # get rid of attributes that do not contain =
        line = re.sub('^#[A-Za-z0-9\(\)\[\]_]*?$', '#[]', line)

        # flag this line if it matches one of the following regular expressions
        # tuple format: (pattern, format_message, filter_function(match, line))
        no_filter = lambda match, line: True
        regex_rules = [
            (r",[^\s]", "missing space after ,",
                lambda match, line: '$' not in line and not is_attribute),
            (r"[A-Za-z0-9\"]=", "missing space before =",
                lambda match, line: is_attribute),
            (r"=[A-Za-z0-9\"]", "missing space after =",
                lambda match, line: is_attribute),
            # ignore scientific notation patterns like 1e-6
            (r"[A-DF-Za-df-z0-9]-", "missing space before -",
                lambda match, line: not is_attribute),
            (r"[A-Za-z0-9]([\+/\*%=])", "missing space before {0}",
                lambda match, line: (not is_attribute and
                                     not is_associated_type(match, line))),
            # * not included because of dereferencing and casting
            # - not included because of unary negation
            (r'([\+/\%=])[A-Za-z0-9"]', "missing space after {0}",
                lambda match, line: (not is_attribute and
                                     not is_associated_type(match, line))),
            (r"\)->", "missing space before ->", no_filter),
            (r"->[A-Za-z]", "missing space after ->", no_filter),
            (r"[^ ]=>", "missing space before =>", lambda match, line: match.start() != 0),
            (r"=>[^ ]", "missing space after =>", lambda match, line: match.end() != len(line)),
            (r"=>  ", "extra space after =>", no_filter),
            # ignore " ::crate::mod" and "trait Foo : Bar"
            (r" :[^:]", "extra space before :",
                lambda match, line: 'trait ' not in line[:match.start()]),
            # ignore "crate::mod" and ignore flagging macros like "$t1:expr"
            (r"[^:]:[A-Za-z0-9\"]", "missing space after :",
                lambda match, line: '$' not in line[:match.end()]),
            (r"[A-Za-z0-9\)]{", "missing space before {{", no_filter),
            # ignore cases like "{}", "}`", "}}" and "use::std::{Foo, Bar}"
            (r"[^\s{}]}[^`]", "missing space before }}",
                lambda match, line: not re.match(r'^(pub )?use', line)),
            # ignore cases like "{}", "`{", "{{" and "use::std::{Foo, Bar}"
            (r"[^`]{[^\s{}]", "missing space after {{",
                lambda match, line: not re.match(r'^(pub )?use', line)),
            # There should not be any extra pointer dereferencing
            (r": &Vec<", "use &[T] instead of &Vec<T>", no_filter),
            # No benefit over using &str
            (r": &String", "use &str instead of &String", no_filter),
            # No benefit to using &Root<T>
            (r": &Root<", "use &T instead of &Root<T>", no_filter),
            (r"^&&", "operators should go at the end of the first line", no_filter),
            (r"\{[A-Za-z0-9_]+\};", "use statement contains braces for single import",
                lambda match, line: line.startswith('use ')),
            (r"^\s*else {", "else braces should be on the same line", no_filter),
            (r"[^$ ]\([ \t]", "extra space after (", no_filter),
        ]

        for pattern, message, filter_func in regex_rules:
            for match in re.finditer(pattern, line):
                if filter_func(match, line):
                    yield (idx + 1, message.format(*match.groups(), **match.groupdict()))

        if prev_open_brace and not line:
            yield (idx + 1, "found an empty line following a {")
        prev_open_brace = line.endswith("{")

        # check alphabetical order of extern crates
        if line.startswith("extern crate "):
            # strip "extern crate " from the begin and ";" from the end
            crate_name = line[13:-1]
            indent = len(original_line) - len(line)
            if indent not in prev_crate:
                prev_crate[indent] = ""
            if prev_crate[indent] > crate_name:
                yield(idx + 1, decl_message.format("extern crate declaration")
                      + decl_expected.format(prev_crate[indent])
                      + decl_found.format(crate_name))
            prev_crate[indent] = crate_name

        # imports must be in the same line, alphabetically sorted, and merged
        # into a single import block
        if line.startswith("use "):
            import_block = True
            indent = len(original_line) - len(line)
            if not line.endswith(";") and '{' in line:
                yield (idx + 1, "use statement spans multiple lines")
            # strip "use" from the begin and ";" from the end
            current_use = line[4:-1]
            if prev_use:
                current_use_cut = current_use.replace("{self,", ".").replace("{", ".")
                prev_use_cut = prev_use.replace("{self,", ".").replace("{", ".")
                if indent == current_indent and current_use_cut < prev_use_cut:
                    yield(idx + 1, decl_message.format("use statement")
                          + decl_expected.format(prev_use)
                          + decl_found.format(current_use))
            prev_use = current_use
            current_indent = indent

        if whitespace or not import_block:
            current_indent = 0

        # do not allow blank lines in an import block
        if import_block and whitespace and line.startswith("use "):
            whitespace = False
            yield(idx, "encountered whitespace following a use statement")

        # modules must be in the same line and alphabetically sorted
        if line.startswith("mod ") or line.startswith("pub mod "):
            indent = len(original_line) - len(line)
            # strip /(pub )?mod/ from the left and ";" from the right
            mod = line[4:-1] if line.startswith("mod ") else line[8:-1]

            if (idx - 1) < 0 or "#[macro_use]" not in lines[idx - 1]:
                match = line.find(" {")
                if indent not in prev_mod:
                    prev_mod[indent] = ""
                if match == -1 and not line.endswith(";"):
                    yield (idx + 1, "mod declaration spans multiple lines")
                if prev_mod[indent] and mod < prev_mod[indent]:
                    yield(idx + 1, decl_message.format("mod declaration")
                          + decl_expected.format(prev_mod[indent])
                          + decl_found.format(mod))
                prev_mod[indent] = mod
        else:
            # we now erase previous entries
            prev_mod = {}


# Avoid flagging <Item=Foo> constructs
def is_associated_type(match, line):
    if match.group(1) != '=':
        return False
    open_angle = line[0:match.end()].rfind('<')
    close_angle = line[open_angle:].find('>') if open_angle != -1 else -1
    generic_open = open_angle != -1 and open_angle < match.start()
    generic_close = close_angle != -1 and close_angle + open_angle >= match.end()
    return generic_open and generic_close


def check_webidl_spec(file_name, contents):
    # Sorted by this function (in pseudo-Rust). The idea is to group the same
    # organization together.
    # fn sort_standards(a: &Url, b: &Url) -> Ordering {
    #     let a_domain = a.domain().split(".");
    #     a_domain.pop();
    #     a_domain.reverse();
    #     let b_domain = b.domain().split(".");
    #     b_domain.pop();
    #     b_domain.reverse();
    #     for i in a_domain.into_iter().zip(b_domain.into_iter()) {
    #         match i.0.cmp(b.0) {
    #             Less => return Less,
    #             Greater => return Greater,
    #             _ => (),
    #         }
    #     }
    #     a_domain.path().cmp(b_domain.path())
    # }

    if not file_name.endswith(".webidl"):
        raise StopIteration

    for i in WEBIDL_STANDARDS:
        if contents.find(i) != -1:
            raise StopIteration
    yield (0, "No specification link found.")


def check_for_possible_duplicate_json_keys(key_value_pairs):
    keys = [x[0] for x in key_value_pairs]
    seen_keys = set()
    for key in keys:
        if key in seen_keys:
            raise KeyError(key)

        seen_keys.add(key)


def check_json(filename, contents):
    if not filename.endswith(".json"):
        raise StopIteration

    try:
        json.loads(contents, object_pairs_hook=check_for_possible_duplicate_json_keys)
    except ValueError as e:
        match = re.search(r"line (\d+) ", e.message)
        line_no = match and match.group(1)
        yield (line_no, e.message)
    except KeyError as e:
        yield (None, "Duplicated Key (%s)" % e.message)


def check_spec(file_name, lines):
    if SPEC_BASE_PATH not in file_name:
        raise StopIteration
    file_name = os.path.relpath(os.path.splitext(file_name)[0], SPEC_BASE_PATH)
    patt = re.compile("^\s*\/\/.+")

    # Pattern representing a line with a macro
    macro_patt = re.compile("^\s*\S+!(.*)$")

    # Pattern representing a line with comment containing a spec link
    link_patt = re.compile("^\s*///? https://.+$")

    # Pattern representing a line with comment
    comment_patt = re.compile("^\s*///?.+$")

    brace_count = 0
    in_impl = False
    pattern = "impl {}Methods for {} {{".format(file_name, file_name)

    for idx, line in enumerate(lines):
        if "// check-tidy: no specs after this line" in line:
            break
        if not patt.match(line):
            if pattern.lower() in line.lower():
                in_impl = True
            if ("fn " in line or macro_patt.match(line)) and brace_count == 1:
                for up_idx in range(1, idx + 1):
                    up_line = lines[idx - up_idx]
                    if link_patt.match(up_line):
                        # Comment with spec link exists
                        break
                    if not comment_patt.match(up_line):
                        # No more comments exist above, yield warning
                        yield (idx + 1, "method declared in webidl is missing a comment with a specification link")
                        break
            if '{' in line and in_impl:
                brace_count += 1
            if '}' in line and in_impl:
                if brace_count == 1:
                    break
                brace_count -= 1


def check_config_file(config_file, print_text=True):
    # Check if config file exists
    if not os.path.exists(config_file):
        print("%s config file is required but was not found" % config_file)
        sys.exit(1)

    # Load configs from servo-tidy.toml
    with open(config_file) as content:
        conf_file = content.read()
        lines = conf_file.splitlines(True)

    if print_text:
        print '\rChecking the config file...'

    current_table = ""
    for idx, line in enumerate(lines):
        # Ignore comment lines
        if line.strip().startswith("#"):
            continue

        # Check for invalid tables
        if re.match("\[(.*?)\]", line.strip()):
            table_name = re.findall(r"\[(.*?)\]", line)[0].strip()
            if table_name not in ("configs", "ignore", "check_ext"):
                yield config_file, idx + 1, "invalid config table [%s]" % table_name
            current_table = table_name
            continue

        # Skip if there is no equal sign in line, assuming it's not a key
        if "=" not in line:
            continue

        key = line.split("=")[0].strip()

        # Check for invalid keys inside [configs] and [ignore] table
        if (current_table == "configs" and key not in config or
                current_table == "ignore" and key not in config["ignore"] or
                # Any key outside of tables
                current_table == ""):
            yield config_file, idx + 1, "invalid config key '%s'" % key

    # Parse config file
    parse_config(conf_file)


def parse_config(content):
    config_file = toml.loads(content)
    exclude = config_file.get("ignore", {})
    # Add list of ignored directories to config
    config["ignore"]["directories"] += exclude.get("directories", [])
    # Add list of ignored files to config
    config["ignore"]["files"] += exclude.get("files", [])
    # Add list of ignored packages to config
    config["ignore"]["packages"] = exclude.get("packages", [])
    # Fix the paths (OS-dependent)
    config['ignore']['files'] = map(lambda path: os.path.join(*path.split('/')),
                                    config['ignore']['files'])
    config['ignore']['directories'] = map(lambda path: os.path.join(*path.split('/')),
                                          config['ignore']['directories'])

    # Add dict of dir, list of expected ext to config
    dirs_to_check = config_file.get("check_ext", {})
    # Fix the paths (OS-dependent)
    for path, exts in dirs_to_check.items():
        fixed_path = os.path.join(*path.split('/'))
        config['check_ext'][fixed_path] = exts

    # Override default configs
    user_configs = config_file.get("configs", [])
    for pref in user_configs:
        if pref in config:
            config[pref] = user_configs[pref]


def check_directory_files(directories, print_text=True):
    if print_text:
        print '\rChecking directories for correct file extensions...'
    for directory, file_extensions in directories.items():
        files = sorted(os.listdir(directory))
        for filename in files:
            if not any(filename.endswith(ext) for ext in file_extensions):
                details = {
                    "name": os.path.basename(filename),
                    "ext": ", ".join(file_extensions),
                    "dir_name": directory
                }
                message = '''Unexpected extension found for {name}. \
We only expect files with {ext} extensions in {dir_name}'''.format(**details)
                yield (filename, 1, message)


def collect_errors_for_files(files_to_check, checking_functions, line_checking_functions, print_text=True):
    (has_element, files_to_check) = is_iter_empty(files_to_check)
    if not has_element:
        raise StopIteration
    if print_text:
        print '\rChecking files for tidiness...'

    for filename in files_to_check:
        if not os.path.exists(filename):
            continue
        with open(filename, "r") as f:
            contents = f.read()
            if not contents.strip():
                yield filename, 0, "file is empty"
                continue
            for check in checking_functions:
                for error in check(filename, contents):
                    # the result will be: `(filename, line, message)`
                    yield (filename,) + error
            lines = contents.splitlines(True)
            for check in line_checking_functions:
                for error in check(filename, lines):
                    yield (filename,) + error


def get_wpt_files(only_changed_files, progress):
    wpt_dir = os.path.join(".", "tests", "wpt" + os.sep)
    file_iter = get_file_list(os.path.join(wpt_dir), only_changed_files)
    (has_element, file_iter) = is_iter_empty(file_iter)
    if not has_element:
        raise StopIteration
    print '\nRunning the WPT lint...'
    if progress:
        file_iter = progress_wrapper(file_iter)
    for f in file_iter:
        if filter_file(f):
            yield f[len(wpt_dir):]


def check_wpt_lint_errors(files):
    wpt_working_dir = os.path.abspath(os.path.join(".", "tests", "wpt", "web-platform-tests"))
    if os.path.isdir(wpt_working_dir):
        site.addsitedir(wpt_working_dir)
        from tools.lint import lint
        returncode = lint.lint(wpt_working_dir, files, output_json=False)
        if returncode:
            yield ("WPT Lint Tool", "", "lint error(s) in Web Platform Tests: exit status {0}".format(returncode))


def get_dep_toml_files(only_changed_files=False):
    if not only_changed_files:
        print '\nRunning the dependency licensing lint...'
        for root, directories, filenames in os.walk(".cargo"):
            for filename in filenames:
                if filename == "Cargo.toml":
                    yield os.path.join(root, filename)


def check_dep_license_errors(filenames, progress=True):
    filenames = progress_wrapper(filenames) if progress else filenames
    for filename in filenames:
        with open(filename, "r") as f:
            ok_licensed = False
            lines = f.readlines()
            for idx, line in enumerate(lines):
                for license_line in licenses_dep_toml:
                    ok_licensed |= (license_line in line)
            if not ok_licensed:
                yield (filename, 0, "dependency should contain a valid license.")


def get_file_list(directory, only_changed_files=False, exclude_dirs=[]):
    if only_changed_files:
        # only check tracked files that have been changed since the last merge
        args = ["git", "log", "-n1", "--author=bors-servo", "--format=%H"]
        last_merge = subprocess.check_output(args).strip()
        args = ["git", "diff", "--name-only", last_merge, directory]
        file_list = subprocess.check_output(args)
        for f in file_list.splitlines():
            f = os.path.join(*f.split("/")) if sys.platform == "win32" else f
            if not any(os.path.join('.', os.path.dirname(f)).startswith(path) for path in exclude_dirs):
                yield os.path.join('.', f)
    elif exclude_dirs:
        for root, dirs, files in os.walk(directory, topdown=True):
            # modify 'dirs' in-place so that we don't do unwanted traversals in excluded directories
            dirs[:] = [d for d in dirs if not any(os.path.join(root, d).startswith(name) for name in exclude_dirs)]
            for rel_path in files:
                yield os.path.join(root, rel_path)
    else:
        for root, _, files in os.walk(directory):
            for f in files:
                yield os.path.join(root, f)


def scan(only_changed_files=False, progress=True):
    # check config file for errors
    config_errors = check_config_file(CONFIG_FILE_PATH)
    # check directories contain expected files
    directory_errors = check_directory_files(config['check_ext'])
    # standard checks
    files_to_check = filter_files('.', only_changed_files, progress)
    checking_functions = (check_flake8, check_lock, check_webidl_spec, check_json)
    line_checking_functions = (check_license, check_by_line, check_toml, check_shell,
                               check_rust, check_spec, check_modeline)
    file_errors = collect_errors_for_files(files_to_check, checking_functions, line_checking_functions)
    # check dependecy licenses
    dep_license_errors = check_dep_license_errors(get_dep_toml_files(only_changed_files), progress)
    # wpt lint checks
    wpt_lint_errors = check_wpt_lint_errors(get_wpt_files(only_changed_files, progress))
    # chain all the iterators
    errors = itertools.chain(config_errors, directory_errors, file_errors, dep_license_errors, wpt_lint_errors)

    error = None
    for error in errors:
        colorama.init()
        print "\r\033[94m{}\033[0m:\033[93m{}\033[0m: \033[91m{}\033[0m".format(*error)

    print
    if error is None:
        colorama.init()
        print "\033[92mtidy reported no errors.\033[0m"

    return int(error is not None)
