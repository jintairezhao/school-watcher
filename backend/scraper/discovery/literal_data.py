"""Read dedicated literal data assignments without executing website JavaScript."""
import re

from .publication_tabs import script_tokens


def string_value(token):
    if len(token) < 2 or token[0] not in "\"'" or token[-1] != token[0]:
        raise ValueError('Expected a quoted string')
    chars, i = [], 1
    escapes = {'b': '\b', 'f': '\f', 'n': '\n', 'r': '\r', 't': '\t',
               '\\': '\\', '/': '/', "'": "'", '"': '"'}
    while i < len(token) - 1:
        char = token[i]
        i += 1
        if char in '\r\n' or char == token[0]:
            raise ValueError('Invalid string character')
        if char != '\\':
            chars.append(char)
            continue
        if i >= len(token) - 1:
            raise ValueError('Incomplete escape')
        char = token[i]
        i += 1
        if char in escapes:
            chars.append(escapes[char])
        elif char in ('u', 'x'):
            length = 4 if char == 'u' else 2
            digits = token[i:i + length]
            if len(digits) != length or not re.fullmatch('[0-9a-fA-F]+', digits):
                raise ValueError('Invalid character escape')
            chars.append(chr(int(digits, 16)))
            i += length
        else:
            raise ValueError('Unsupported escape')
    try:
        return ''.join(chars).encode('utf-16-le', 'surrogatepass').decode('utf-16-le')
    except UnicodeError as exc:
        raise ValueError('Unpaired surrogate') from exc


def literal_assignment(script, binding):
    """Allow only a single exact assignment of strings, objects and arrays.

    The reviewed templates need no expressions, calls, numbers or interpolated
    strings. Reject the whole binding on unsupported syntax or duplicate keys.
    """
    if len(script) > 500000:
        raise ValueError('Literal data exceeds size limit')
    tokens = script_tokens(script)
    prefix = tuple(part for name in binding.split('.') for part in (name, '.'))[:-1] + ('=',)
    if len(tokens) > 50000 or tokens[:len(prefix)] != prefix:
        raise ValueError('Not a dedicated literal assignment')
    cursor = len(prefix)

    def take():
        nonlocal cursor
        if cursor >= len(tokens):
            raise ValueError('Unexpected end of literal data')
        token = tokens[cursor]
        cursor += 1
        return token

    def value(depth=0):
        nonlocal cursor
        if depth > 20:
            raise ValueError('Literal data exceeds nesting limit')
        token = take()
        if token.startswith(('"', "'")):
            return string_value(token)
        if token not in ('[', '{'):
            raise ValueError('Unsupported literal value')
        closing = ']' if token == '[' else '}'
        result = [] if token == '[' else {}
        while cursor < len(tokens) and tokens[cursor] != closing:
            if isinstance(result, dict):
                key = take()
                if key.startswith(('"', "'")):
                    key = string_value(key)
                elif not re.fullmatch(r'[A-Za-z_$][A-Za-z0-9_$]*', key):
                    raise ValueError('Invalid object key')
                if key in result or key in ('__proto__', 'constructor', 'prototype') or take() != ':':
                    raise ValueError('Duplicate or invalid object key')
                result[key] = value(depth + 1)
            else:
                result.append(value(depth + 1))
            if cursor < len(tokens) and tokens[cursor] == closing:
                break
            if take() != ',':
                raise ValueError('Expected comma')
        if take() != closing:
            raise ValueError('Unclosed literal container')
        return result

    result = value()
    if tokens[cursor:] not in ((), (';',)):
        raise ValueError('Trailing executable or unsupported content')
    return result
