"""Select an exact refusal site from the source of the package being called.

Compaction moves an abort into a private boolean guard. Never broaden an effects
matcher to an arbitrary function or code: resolve one site before execution.
"""
from pathlib import Path
import re
import sys


def abort_site(package, module, function, code):
    source = (Path(package) / 'sources' / (module + '.move')).read_text()
    # Private renames retain a reviewed original-name comment in release source.
    alias = re.search(r'/// ' + re.escape(function) + r':[^\n]*\n\s*fun (\w+)\(', source)
    name = alias[1] if alias else function
    source = re.sub(r'/\*.*?\*/|//[^\n]*', '', source, flags=re.S)
    constants = re.findall(r'const (E\w+): u\d+ = ' + str(code) + r';', source)
    functions = {}
    for start in re.finditer(r'\bfun (\w+)\b[^\{]*\{', source):
        depth, end = 1, start.end()
        while depth and end < len(source):
            depth += (source[end] == '{') - (source[end] == '}')
            end += 1
        functions[start[1]] = source[start.end():end-1]
    if name not in functions:
        raise ValueError(f'missing abort function {module}::{function}')

    def sites(current, seen):
        if current in seen:
            return set()
        body = functions[current]
        direct = set()
        for constant in constants:
            if re.search(r'\b' + constant + r'\b', body):
                direct.add(current)
            for guard in re.finditer(r'fun (\w+)\(condition: bool\)\s*\{\s*if \(!condition\) abort ' + constant + r'\s*\}', source):
                if re.search(r'(?<![:\w])' + guard[1] + r'\(', body):
                    direct.add(guard[1])
        if direct:
            return direct
        # A few historical private refusal sites now forward to another local
        # helper. Follow only local calls, never another module's same code.
        result = set()
        for call in re.findall(r'(?<![:\w])(\w+)\(', body):
            if call in functions:
                result.update(sites(call, seen | {current}))
        return result

    candidates = sites(name, set())
    if len(candidates) != 1:
        raise ValueError(f'unrecognized/ambiguous abort {module}::{function} {code}: {candidates}')
    return candidates.pop()


if __name__ == '__main__':
    print(abort_site(*sys.argv[1:4], int(sys.argv[4])))
