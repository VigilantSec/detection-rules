# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one
# or more contributor license agreements. Licensed under the Elastic License
# 2.0; you may not use this file except in compliance with the Elastic License
# 2.0.

"""Helper functions for managing rules in the repository."""
import copy
import dataclasses
import io
import json
import re
import textwrap
import typing
from collections import OrderedDict

import toml

from .schemas import definitions
from .utils import cached

SQ = "'"
DQ = '"'
TRIPLE_SQ = SQ * 3
TRIPLE_DQ = DQ * 3


@cached
def get_preserved_fmt_fields():
    from .rule import BaseRuleData
    preserved_keys = set(['message'])

    for field in dataclasses.fields(BaseRuleData):  # type: dataclasses.Field
        if field.type in (definitions.Markdown, typing.Optional[definitions.Markdown]):
            preserved_keys.add(field.metadata.get("data_key", field.name))
    return preserved_keys


def cleanup_whitespace(val):
    if isinstance(val, str):
        return " ".join(line.strip() for line in val.strip().splitlines())
    return val


def nested_normalize(d, skip_cleanup=False):
    if isinstance(d, str):
        return d if skip_cleanup else cleanup_whitespace(d)
    elif isinstance(d, list):
        return [nested_normalize(val) for val in d]
    elif isinstance(d, dict):
        for k, v in d.items():
            if k == 'query':
                # TODO: the linter still needs some work, but once up to par, uncomment to implement - kql.lint(v)
                # do not normalize queries
                d.update({k: v})
            elif k in get_preserved_fmt_fields():
                # let these maintain newlines and whitespace for markdown support
                d.update({k: nested_normalize(v, skip_cleanup=True)})
            else:
                d.update({k: nested_normalize(v)})
        return d
    else:
        return d


def wrap_text(v, block_indent=0, join=False):
    """Block and indent a blob of text."""
    v = ' '.join(v.split())
    lines = textwrap.wrap(v, initial_indent=' ' * block_indent, subsequent_indent=' ' * block_indent, width=120,
                          break_long_words=False, break_on_hyphens=False)
    lines = [line + '\n' for line in lines]
    # If there is a single line that contains a quote, add a new blank line to trigger multiline formatting
    if len(lines) == 1 and '"' in lines[0]:
        lines = lines + ['']
    return lines if not join else ''.join(lines)


class NonformattedField(str):
    """Non-formatting class."""


def preserve_formatting_for_fields(data: OrderedDict, fields_to_preserve: list) -> OrderedDict:
    """Preserve formatting for specified nested fields in an action."""

    def apply_preservation(target: OrderedDict, keys: list) -> None:
        """Apply NonformattedField preservation based on keys path."""
        for key in keys[:-1]:
            # Iterate to the key, diving into nested dictionaries
            if key in target and isinstance(target[key], dict):
                target = target[key]
            else:
                # Cannot preserve formatting for missing or non-dict intermediate
                return

        final_key = keys[-1]
        if final_key in target:
            # Apply NonformattedField to the target field if it exists
            target[final_key] = NonformattedField(target[final_key])

    for field_path in fields_to_preserve:
        keys = field_path.split('.')
        apply_preservation(data, keys)

    return data


class RuleTomlEncoder(toml.TomlEncoder):
    """Generate a pretty form of toml."""

    def __init__(self, _dict=dict, preserve=False):
        """Create the encoder but override some default functions."""
        super(RuleTomlEncoder, self).__init__(_dict, preserve)
        self._old_dump_str = toml.TomlEncoder().dump_funcs[str]
        self._old_dump_list = toml.TomlEncoder().dump_funcs[list]
        self.dump_funcs[str] = self.dump_str
        self.dump_funcs[type(u"")] = self.dump_str
        self.dump_funcs[list] = self.dump_list
        self.dump_funcs[NonformattedField] = self.dump_str

    def dump_str(self, v):
        """Change the TOML representation to multi-line or single quote when logical."""
        initial_newline = ['\n']

        if isinstance(v, NonformattedField):
            # first line break is not forced like other multiline string dumps
            lines = v.splitlines(True)
            initial_newline = []

        else:
            lines = wrap_text(v)

        multiline = len(lines) > 1
        raw = (multiline or (DQ in v and SQ not in v)) and TRIPLE_DQ not in v

        if multiline:
            if raw:
                return re.sub(r'(?<!\\)\\(?!\\)', r'\\\\', "".join([TRIPLE_DQ] + initial_newline + lines + [TRIPLE_DQ]))
            else:
                return "\n".join([TRIPLE_SQ] + [self._old_dump_str(line)[1:-1] for line in lines] + [TRIPLE_SQ])
        elif raw:
            return u"'{:s}'".format(lines[0])
        return self._old_dump_str(v)

    def _dump_flat_list(self, v):
        """A slightly tweaked version of original dump_list, removing trailing commas."""
        if not v:
            return "[]"

        retval = "[" + str(self.dump_value(v[0])) + ","
        for u in v[1:]:
            retval += " " + str(self.dump_value(u)) + ","
        retval = retval.rstrip(',') + "]"
        return retval

    def dump_list(self, v):
        """Dump a list more cleanly."""
        if all([isinstance(d, str) for d in v]) and sum(len(d) + 3 for d in v) > 100:
            dump = []
            for item in v:
                if len(item) > (120 - 4 - 3 - 3) and ' ' in item:
                    dump.append('    """\n{}    """'.format(wrap_text(item, block_indent=4, join=True)))
                else:
                    dump.append(' ' * 4 + self.dump_value(item))
            return '[\n{},\n]'.format(',\n'.join(dump))

        if v and all(isinstance(i, dict) for i in v):
            # Compact inline format for lists of dictionaries with proper indentation
            retval = "\n" + ' ' * 2 + "[\n"
            retval += ",\n".join([' ' * 4 + self.dump_inline_table(u).strip() for u in v])
            retval += "\n" + ' ' * 2 + "]\n"
            return retval

        return self._dump_flat_list(v)


def toml_write(rule_contents, outfile=None):
    """Write rule in TOML."""
    def write(text, nl=True):
        if outfile:
            outfile.write(text)
            if nl:
                outfile.write(u"\n")
        else:
            print(text, end='' if not nl else '\n')

    encoder = RuleTomlEncoder()
    contents = copy.deepcopy(rule_contents)
    needs_close = False

    def order_rule(obj):
        if isinstance(obj, dict):
            obj = OrderedDict(sorted(obj.items()))
            for k, v in obj.items():
                if isinstance(v, dict) or isinstance(v, list):
                    obj[k] = order_rule(v)

        if isinstance(obj, list):
            for i, v in enumerate(obj):
                if isinstance(v, dict) or isinstance(v, list):
                    obj[i] = order_rule(v)
            obj = sorted(obj, key=lambda x: json.dumps(x))

        return obj

    def _do_write(_data, _contents):
        query = None
        threat_query = None

        if _data == 'rule':
            # - We want to avoid the encoder for the query and instead use kql-lint.
            # - Linting is done in rule.normalize() which is also called in rule.validate().
            # - Until lint has tabbing, this is going to result in all queries being flattened with no wrapping,
            #     but will at least purge extraneous white space
            query = contents['rule'].pop('query', '').strip()

            # - As tags are expanding, we may want to reconsider the need to have them in alphabetical order
            # tags = contents['rule'].get("tags", [])
            #
            # if tags and isinstance(tags, list):
            #     contents['rule']["tags"] = list(sorted(set(tags)))
            threat_query = contents['rule'].pop('threat_query', '').strip()

        top = OrderedDict()
        bottom = OrderedDict()

        for k in sorted(list(_contents)):
            v = _contents.pop(k)

            if k == 'actions':
                # explicitly preserve formatting for message field in actions
                preserved_fields = ["params.message"]
                v = [preserve_formatting_for_fields(action, preserved_fields) for action in v] if v is not None else []

            if k == 'filters':
                # explicitly preserve formatting for value field in filters
                preserved_fields = ["meta.value"]
                v = [preserve_formatting_for_fields(meta, preserved_fields) for meta in v] if v is not None else []

            if k == 'note' and isinstance(v, str):
                # Transform instances of \ to \\ as calling write will convert \\ to \.
                # This will ensure that the output file has the correct number of backslashes.
                v = v.replace("\\", "\\\\")

            if k == 'setup' and isinstance(v, str):
                # Transform instances of \ to \\ as calling write will convert \\ to \.
                # This will ensure that the output file has the correct number of backslashes.
                v = v.replace("\\", "\\\\")

            if k == 'description' and isinstance(v, str):
                # Transform instances of \ to \\ as calling write will convert \\ to \.
                # This will ensure that the output file has the correct number of backslashes.
                v = v.replace("\\", "\\\\")

            if k == 'osquery' and isinstance(v, list):
                # Specifically handle transform.osquery queries
                for osquery_item in v:
                    if 'query' in osquery_item and isinstance(osquery_item['query'], str):
                        # Transform instances of \ to \\ as calling write will convert \\ to \.
                        # This will ensure that the output file has the correct number of backslashes.
                        osquery_item['query'] = osquery_item['query'].replace("\\", "\\\\")

            if isinstance(v, dict):
                bottom[k] = OrderedDict(sorted(v.items()))
            elif isinstance(v, list):
                if any([isinstance(value, (dict, list)) for value in v]):
                    bottom[k] = v
                else:
                    top[k] = v
            elif k in get_preserved_fmt_fields():
                top[k] = NonformattedField(v)
            else:
                top[k] = v

        if query:
            top.update({'query': "XXxXX"})

        if threat_query:
            top.update({'threat_query': "XXxXX"})

        top.update(bottom)
        top = toml.dumps(OrderedDict({data: top}), encoder=encoder)

        # we want to preserve the threat_query format, but want to modify it in the context of encoded dump
        if threat_query:
            formatted_threat_query = "\nthreat_query = '''\n{}\n'''{}".format(threat_query, '\n\n' if bottom else '')
            top = top.replace('threat_query = "XXxXX"', formatted_threat_query)

        # we want to preserve the query format, but want to modify it in the context of encoded dump
        if query:
            formatted_query = "\nquery = '''\n{}\n'''{}".format(query, '\n\n' if bottom else '')
            top = top.replace('query = "XXxXX"', formatted_query)

        write(top)

    try:

        if outfile and not isinstance(outfile, io.IOBase):
            needs_close = True
            outfile = open(outfile, 'w')

        for data in ('metadata', 'transform', 'rule'):
            _contents = contents.get(data, {})
            if not _contents:
                continue
            order_rule(_contents)
            _do_write(data, _contents)

    finally:
        if needs_close and hasattr(outfile, "close"):
            outfile.close()
