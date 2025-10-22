from io import StringIO
import builtins
import keyword
import bisect
import time
import re

from idlelib.config import idleConf
from idlelib.delegator import Delegator

DEBUG = False


class Buffer(StringIO):
    __slots__ = "total_data", "_newlines"

    def __init__(self, data):
        super().__init__(data)
        self.total_data = data
        self._newlines = [0] + \
                         [i for i,c in enumerate(data) if c == "\n"] + \
                         [len(data)]

    def peek(self, size):
        location = super().tell()
        return self.total_data[location:location+size]

    def closest_newlines(self, index):
        "Get the location of the closest newlines around `index`"
        if not index:
            return 0, self._newlines[1]
        idx = bisect.bisect_left(self._newlines, index)
        low_idx = idx - 1
        while low_idx > 0:
            start_text_idx = self._newlines[low_idx-1]
            end_text_idx = self._newlines[low_idx]
            line = self.total_data[start_text_idx:end_text_idx]
            slashes = len(line) - len(line.rstrip("\\"))
            if not (slashes&1): break
            low_idx -= 1
        return self._newlines[low_idx], self._newlines[idx]

    def __bool__(self):
        return super().tell() != len(self.total_data)


REPLACE_LINE_CONTINUATIONS = re.compile(
    r"[ \t]*" +      # Any white spaces, followed by
    r"(?<!\\)" +     # Not followed by a slash, followed by
    r"((?:\\\\)*)" + # An even number of slashes, followed by
    r"\\\n" +        # A slash and a newline, followed by
    r"[ \t]*"        # Any indentation
)


class Parser:
    __slots__ = "_under", "_tags", "_start_size_map", "_peeked_token"

    def __bool__(self):
        "Test if there are any tokens left in the buffer"
        return bool(self._peeked_token or self._under)

    def curr_line_seen(self):
        "Get the text on the current line that has been read already."
        curr = self.tell()
        start, end = self._under.closest_newlines(curr)
        end = end if curr is None else curr
        text = self._under.total_data[start:end].removeprefix("\n")
        if "\n" in text:
            text = REPLACE_LINE_CONTINUATIONS.sub(r"\1", text)
        return text

    def tell(self):
        # Subtract the peeked token because it was actually read not peeked
        return self._under.tell() - len(self._peeked_token or "")

    def peek_token(self):
        "Peek a token (indentation/identifier/number/any other character)"
        if self._peeked_token is None:
            start = self._under.tell()
            self._peeked_token = self._pure_read_token()
            if not self._peeked_token: return ""
            self._start_size_map.append((start,len(self._peeked_token)))
            self._tags[start] = "SYNC"*(self._peeked_token == "\n")
        return self._peeked_token

    def skip_whitespaces(self):
        while self and (not self.peek_token().strip(" \t")):
            self.skip()

    def _pure_read_token(self):
        output = self._under.read(1)
        if output:
            if output in " \t":
                while True:
                    if self._under.peek(1) not in " \t": break
                    output += self._under.read(1)
                return output
            elif output.isidentifier():
                while (output + self._under.peek(1)).isidentifier():
                    output += self._under.read(1)
                return output
            elif output.isdigit():
                # Note that leading -/+ signs aren't part of the token
                while (output + self._under.peek(1)).isdigit():
                    output += self._under.read(1)
                return output
        return output

    def set(self, tokentype, index=None):
        """
        Set the token at `index`'s type as `tokentype`. If index is `None`,
        `index` is assumed to be `self.tell()`.
        If `index` is `None` or at `self.tell()`, it sets the tokentype and
        moves the buffer forward
        """
        curr = self.tell()
        if index is None:
            index = curr
        self._tags[index] = tokentype
        if index == curr:
            self.skip()

    def set_from(self, replace_tokentype, start, *, ignoretypes=()):
        """
        Replace all tokentypes from `start` until `self.tell()` with
        `replace_tokentype` if the tokentype is not in `ignoretypes`
        """
        end = self.tell()
        idx = bisect.bisect_left(self._start_size_map, (start,))
        while start < end:
            if not (ignoretypes and (self._tags[start] in ignoretypes)):
                self.set(replace_tokentype, start)
            start = sum(self._start_size_map[idx])
            idx += 1

    def skip(self):
        "Skip a token"
        self._peeked_token = None
        self.peek_token()

    def read_wait_for(self, tokens, settype=None, *, ignoretypes=()):
        """
        Calls `self.read()` until the next token is in the `tokens` iterable
          passed in. If `settype` is passed in, this function behaves like
          `set_from` for all of the tokens read (excluding the returned one)
        Returns the next token (guaranteed to be empty or one of `tokens`)
        """
        while True:
            token = self.peek_token()
            if (token in tokens) or (not token):
                return token
            elif token == "\n":
                self.set(f"no-sync-{settype}")
            else:
                if settype is None:
                    self.read()
                else:
                    start = self.tell()
                    self.read()
                    self.set_from(settype, start, ignoretypes=ignoretypes)

    def _master_read(self, text):
        assert text.endswith("\n"), "self._pure_read_token might loop forever"
        # Reset
        self._under = Buffer(text)
        self._start_size_map = []
        self._peeked_token = None
        self._tags = {}
        # Read tokens
        while self.peek_token(): self.read()
        assert self.tell() == len(text), "InternalError"
        # Merge and yield tags
        max_idx = len(self._start_size_map)
        idx = 0
        while idx < max_idx:
            # Get info
            start, size = self._start_size_map[idx]
            tokentype = self._tags[start]
            end = start + size
            idx += 1
            if not tokentype: continue
            while tokentype == self._tags.get(end, None):
                end += self._start_size_map[idx][1]
                idx += 1
            yield start, end, tokentype
        assert end == len(self._under.total_data), "InternalError"


CMD_KWS = frozenset({"assert", "with", "async", "def", "class", "break",
                    "continue", "del", "elif", "try", "except", "finally",
                    "from", "import", "nonlocal", "global", "pass", "raise",
                    "return", "while", "for"})
NOT_AFTER_SOFT_KW = frozenset(":,;=^&|@~)]}")
STRING_PREFIXES = frozenset({"r","u","f","t","b","fr","rf","tr","rt","br","rb"})
KEYWORDS = frozenset(keyword.kwlist)
BUILTINS = frozenset({name for name in dir(builtins)
                      if not name.startswith("_") and name not in KEYWORDS})


class PyParser(Parser):
    __slots__ = ()

    def read(self):
        token = self.peek_token()
        if not token: return None
        if token in KEYWORDS:
            self.set("KEYWORD")
            if token in ("def", "class"):
                self.skip_whitespaces()
                if self.peek_token().isidentifier():
                    self.set("DEFINITION")
        elif token in BUILTINS:
            if self.curr_line_seen().rstrip(" \t")[-1:] == ".":
                self.skip()
            else:
                self.set("BUILTIN")
        elif token == "{":
            self.skip()
            if self.read_wait_for("}") == "}":
                self.skip()
        elif token == "#":
            while self.peek_token() != "\n":
                self.set("COMMENT")
        elif token.lower() in STRING_PREFIXES:
            start = self.tell()
            self.skip()
            new_token = self.peek_token()
            if new_token in "'\"":
                self.set("STRING", start)
                self.read_string(token)
        elif token in "'\"":
            self.read_string("")
        elif token == "match":
            start = self.tell()
            self.skip()
            if not self.curr_line_seen().rstrip(" \t"):
                self.skip_whitespaces()
                if self.peek_token() not in NOT_AFTER_SOFT_KW | KEYWORDS:
                    self.set("KEYWORD", start)
        elif token == "case":
            start = self.tell()
            self.skip()
            if not self.curr_line_seen().rstrip(" \t"):
                self.skip_whitespaces()
                if self.peek_token() not in NOT_AFTER_SOFT_KW | KEYWORDS:
                    self.set("KEYWORD", start)
                    if new_token == "_":
                        self.set("KEYWORD")
        elif token == "\\":
            self.skip()
            token = self.peek_token()
            if token == "\n":
                self.set("no-sync-backslash")
            elif token == "\\":
                self.skip()
        else:
            self.skip()

    def read_string(self, prefix):
        fstring = "f" in prefix
        single = self.peek_token()
        self.set("STRING")
        triple = False
        if self.peek_token() == single:
            self.set("STRING")
            if self.peek_token() != single:
                return None
            self.set("STRING")
            triple = True
        while True:
            token = self.peek_token()
            if not token:
                break
            elif (not triple) and (token == "\n"):
                break
            elif token == "\\":
                self.set("STRING")
                if fstring and (self.peek_token() in "{}"):
                    continue
                self.set("STRING")
            elif token == single:
                self.set("STRING")
                if not triple: break
                if self.peek_token() == single:
                    self.set("STRING")
                    if self.peek_token() == single:
                        self.set("STRING")
                        break
            elif fstring and (token == "{"):
                self.set("STRING")
                if self.peek_token() == "{":
                    self.set("STRING")
                    continue
                start = self.tell()
                token = self.read_wait_for({"}"} | CMD_KWS)
                self.set_from("STRING", start)
                if token == "}":
                    self.set("STRING")
                else:
                    return None
            else:
                self.set("STRING")


def color_config(text):
    """Set color options of Text widget.

    If ColorDelegator is used, this should be called first.
    """
    # Called from htest, TextFrame, Editor, and Turtledemo.
    # Not automatic because ColorDelegator does not know 'text'.
    theme = idleConf.CurrentTheme()
    normal_colors = idleConf.GetHighlight(theme, 'normal')
    cursor_color = idleConf.GetHighlight(theme, 'cursor')['foreground']
    select_colors = idleConf.GetHighlight(theme, 'hilite')
    text.config(
        foreground=normal_colors['foreground'],
        background=normal_colors['background'],
        insertbackground=cursor_color,
        selectforeground=select_colors['foreground'],
        selectbackground=select_colors['background'],
        inactiveselectbackground=select_colors['background'],  # new in 8.5
        )


class ColorDelegator(Delegator):
    """Delegator for syntax highlighting (text coloring).

    Instance variables:
        delegate: Delegator below this one in the stack, meaning the
                one this one delegates to.

        Used to track state:
        after_id: Identifier for scheduled after event, which is a
                timer for colorizing the text.
        allow_colorizing: Boolean toggle for applying colorizing.
        colorizing: Boolean flag when colorizing is in process.
        stop_colorizing: Boolean flag to end an active colorizing
                process.
    """

    def __init__(self):
        Delegator.__init__(self)
        self.init_state()
        self.LoadTagDefs()

    def init_state(self):
        "Initialize variables that track colorizing state."
        self.after_id = None
        self.allow_colorizing = True
        self.stop_colorizing = False
        self.colorizing = False

    def setdelegate(self, delegate):
        """Set the delegate for this instance.

        A delegate is an instance of a Delegator class and each
        delegate points to the next delegator in the stack.  This
        allows multiple delegators to be chained together for a
        widget.  The bottom delegate for a colorizer is a Text
        widget.

        If there is a delegate, also start the colorizing process.
        """
        if self.delegate is not None:
            self.unbind("<<toggle-auto-coloring>>")
        Delegator.setdelegate(self, delegate)
        if delegate is not None:
            self.config_colors()
            self.bind("<<toggle-auto-coloring>>", self.toggle_colorize_event)
            self.notify_range("1.0", "end")
        else:
            # No delegate - stop any colorizing.
            self.stop_colorizing = True
            self.allow_colorizing = False

    def config_colors(self):
        "Configure text widget tags with colors from tagdefs."
        for tag, cnf in self.tagdefs.items():
            self.tag_configure(tag, **cnf)
        self.tag_raise('sel')

    def LoadTagDefs(self):
        "Create dictionary of tag names to text colors."
        theme = idleConf.CurrentTheme()
        self.tagdefs = {
            "COMMENT": idleConf.GetHighlight(theme, "comment"),
            "KEYWORD": idleConf.GetHighlight(theme, "keyword"),
            "BUILTIN": idleConf.GetHighlight(theme, "builtin"),
            "STRING": idleConf.GetHighlight(theme, "string"),
            "DEFINITION": idleConf.GetHighlight(theme, "definition"),
            "SYNC": {'background': None, 'foreground': None},
            "TODO": {'background': None, 'foreground': None},
            "ERROR": idleConf.GetHighlight(theme, "error"),
            # "hit" is used by ReplaceDialog to mark matches. It shouldn't be changed by Colorizer, but
            # that currently isn't technically possible. This should be moved elsewhere in the future
            # when fixing the "hit" tag's visibility, or when the replace dialog is replaced with a
            # non-modal alternative.
            "hit": idleConf.GetHighlight(theme, "hit"),
            }
        if DEBUG: print('tagdefs', self.tagdefs)

    def insert(self, index, chars, tags=None):
        "Insert chars into widget at index and mark for colorizing."
        index = self.index(index)
        self.delegate.insert(index, chars, tags)
        self.notify_range(index, index + "+%dc" % len(chars))

    def delete(self, index1, index2=None):
        "Delete chars between indexes and mark for colorizing."
        index1 = self.index(index1)
        self.delegate.delete(index1, index2)
        self.notify_range(index1)

    def notify_range(self, index1, index2=None):
        "Mark text changes for processing and restart colorizing, if active."
        self.tag_add("TODO", index1, index2)
        if self.after_id:
            if DEBUG: print("colorizing already scheduled")
            return
        if self.colorizing:
            self.stop_colorizing = True
            if DEBUG: print("stop colorizing")
        if self.allow_colorizing:
            if DEBUG: print("schedule colorizing")
            self.after_id = self.after(1, self.recolorize)
        return

    def close(self):
        if self.after_id:
            after_id = self.after_id
            self.after_id = None
            if DEBUG: print("cancel scheduled recolorizer")
            self.after_cancel(after_id)
        self.allow_colorizing = False
        self.stop_colorizing = True

    def toggle_colorize_event(self, event=None):
        """Toggle colorizing on and off.

        When toggling off, if colorizing is scheduled or is in
        process, it will be cancelled and/or stopped.

        When toggling on, colorizing will be scheduled.
        """
        if self.after_id:
            after_id = self.after_id
            self.after_id = None
            if DEBUG: print("cancel scheduled recolorizer")
            self.after_cancel(after_id)
        if self.allow_colorizing and self.colorizing:
            if DEBUG: print("stop colorizing")
            self.stop_colorizing = True
        self.allow_colorizing = not self.allow_colorizing
        if self.allow_colorizing and not self.colorizing:
            self.after_id = self.after(1, self.recolorize)
        if DEBUG:
            print("auto colorizing turned",
                  "on" if self.allow_colorizing else "off")
        return "break"

    def recolorize(self):
        """Timer event (every 1ms) to colorize text.

        Colorizing is only attempted when the text widget exists,
        when colorizing is toggled on, and when the colorizing
        process is not already running.

        After colorizing is complete, some cleanup is done to
        make sure that all the text has been colorized.
        """
        self.after_id = None
        if not self.delegate:
            if DEBUG: print("no delegate")
            return
        if not self.allow_colorizing:
            if DEBUG: print("auto colorizing is off")
            return
        if self.colorizing:
            if DEBUG: print("already colorizing")
            return
        try:
            self.stop_colorizing = False
            self.colorizing = True
            if DEBUG: print("colorizing...")
            t0 = time.perf_counter()
            self.recolorize_main()
            t1 = time.perf_counter()
            if DEBUG: print("%.3f seconds" % (t1-t0))
        finally:
            self.colorizing = False
        if self.allow_colorizing and self.tag_nextrange("TODO", "1.0"):
            if DEBUG: print("reschedule colorizing")
            self.after_id = self.after(1, self.recolorize)

    def recolorize_main(self):
        "Evaluate text and apply colorizing tags."

        # Shortcut for when we are recoloring everything
        # Usually this happens when we open a file
        todo_tag_range = self.tag_nextrange("TODO", "1.0")
        if not todo_tag_range:
            return None
        start, end = todo_tag_range
        if (start == "1.0") and self.compare(end, "==", "end"):
            self.removecolors()
            self._add_tags_in_section(self.get("1.0", "end"), "1.0")
            return None

        next = "1.0"
        while todo_tag_range := self.tag_nextrange("TODO", next):
            self.tag_remove("SYNC", todo_tag_range[0], todo_tag_range[1])
            sync_tag_range = self.tag_prevrange("SYNC", todo_tag_range[0])
            head = sync_tag_range[1] if sync_tag_range else "1.0"

            chars = ""
            next = head
            lines_to_get = 1
            ok = False
            while not ok:
                mark = next
                next = self.index(mark + "+%d lines linestart" %
                                         lines_to_get)
                lines_to_get = min(lines_to_get * 2, 100)
                ok = "SYNC" in self.tag_names(next + "-1c")
                line = self.get(mark, next)
                ##print head, "get", mark, next, "->", repr(line)
                if not line:
                    return
                for tag in self.tagdefs:
                    self.tag_remove(tag, mark, next)
                chars += line
                self._add_tags_in_section(chars, head)
                if "SYNC" in self.tag_names(next + "-1c"):
                    head = next
                    chars = ""
                else:
                    ok = False
                if not ok:
                    # We're in an inconsistent state, and the call to
                    # update may tell us to stop.  It may also change
                    # the correct value for "next" (since this is a
                    # line.col string, not a true mark).  So leave a
                    # crumb telling the next invocation to resume here
                    # in case update tells us to leave.
                    self.tag_add("TODO", next)
                self.update_idletasks()
                if self.stop_colorizing:
                    if DEBUG: print("colorizing stopped")
                    return

    def _add_tag(self, start, end, head, matched_group_name):
        """Add a tag to a given range in the text widget.

        This is a utility function, receiving the range as `start` and
        `end` positions, each of which is a number of characters
        relative to the given `head` index in the text widget.

        The tag to add is determined by `matched_group_name`, which is
        the name of a regular expression "named group" as matched by
        by the relevant highlighting regexps.
        """

    def _add_tags_in_section(self, chars, head):
        """Parse and add highlighting tags to a given part of the text.

        `chars` is a string with the text to parse and to which
        highlighting is to be applied.

        `head` is the index in the text widget where the text is found.
        """
        subtract_chars = 0
        for start, end, tag in PyParser()._master_read(chars):
            head = self.index(f"{head}+{start-subtract_chars:d}c")
            subtract_chars = start
            self.tag_add(tag, head, f"{head}+{end-subtract_chars:d}c")

    def removecolors(self):
        "Remove all colorizing tags."
        for tag in self.tagdefs:
            self.tag_remove(tag, "1.0", "end")


def _color_delegator(parent):  # htest #
    from tkinter import Toplevel, Text
    from idlelib.idle_test.test_colorizer import source
    from idlelib.percolator import Percolator

    top = Toplevel(parent)
    top.title("Test ColorDelegator")
    x, y = map(int, parent.geometry().split('+')[1:])
    top.geometry("700x550+%d+%d" % (x + 20, y + 175))

    text = Text(top, background="white")
    text.pack(expand=1, fill="both")
    text.insert("insert", source)
    text.focus_set()

    color_config(text)
    p = Percolator(text)
    d = ColorDelegator()
    p.insertfilter(d)


if __name__ == "__main__":
    from unittest import main
    main('idlelib.idle_test.test_colorizer', verbosity=2, exit=False)

    from idlelib.idle_test.htest import run
    run(_color_delegator)
