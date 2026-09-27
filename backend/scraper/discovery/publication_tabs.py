"""Bind a publication panel to its explicit control; never infer tab order."""
import re
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class TabBinding:
    panel: object
    control: object
    column_link: object = None
    method: str = 'explicit_tab_control'
    column_href: str = ''
    column_script: object = None
    tab_index: int = -1
    group_control: object = None


def script_tokens(script):
    """Keep strings as indivisible tokens; commented or quoted code is not a handler."""
    tokens = re.findall(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|/\*[\s\S]*?\*/|//[^\r\n]*|[\w$]+|[^\s]''', script)
    return tuple(t for t in tokens if not t.startswith(('/*', '//')))


def occurrences(tokens, pattern):
    return sum(tokens[i:i + len(pattern)] == pattern for i in range(len(tokens) - len(pattern) + 1))


def office_indexed_tabs(soup, url):
    parsed = urlsplit(url)
    if parsed.hostname != 'dangzheng.nwpu.edu.cn' or parsed.path not in ('/', '/index.htm'):
        return {}
    modules = soup.select('.sect1-r')
    if len(modules) != 1:
        return {}
    module = modules[0]
    controls = module.select('.title h2 span')
    panels = module.select('.s1-qh ul')
    more = module.select('.title .more a')
    # This exact official handler defines the index relationship between three
    # separate sibling collections. Merely finding equal-sized lists is not proof.
    handler = script_tokens('''$(".sect1-r .title h2 span").mouseenter(function() {
        $(this).addClass("on").siblings().removeClass("on");
        var i = $(this).index();
        $(".sect1-r .s1-qh ul").eq(i).addClass("on").siblings().removeClass("on");
        $(".sect1-r .title .more a").eq(i).addClass("on").siblings().removeClass("on");
    })''')
    scripts = [script_tokens(s.get_text()) for s in soup.find_all('script', src=False)]
    header = script_tokens('$(".sect1-r .title h2 span").mouseenter(')
    valid = (sum(occurrences(s, handler) for s in scripts) == 1
             and sum(occurrences(s, header) for s in scripts) == 1
             and len(controls) == len(panels) == len(more) > 1
             and all(c.parent is controls[0].parent for c in controls)
             and list(controls[0].parent.find_all(recursive=False)) == controls)
    bindings = {}
    for index, panel in enumerate(panels):
        binding = TabBinding(panel, controls[index] if valid else None,
                             more[index] if valid else None, 'official_indexed_tab_control')
        bindings[id(panel)] = binding
        if valid:
            bindings[id(more[index])] = binding
    if not valid:
        for link in more:
            bindings[id(link)] = TabBinding(module, None)
    return bindings


def language_indexed_tabs(soup, url):
    """Bind the reviewed handler's literal branches without executing JavaScript."""
    parsed = urlsplit(url)
    if parsed.hostname != 'sfl.swjtu.edu.cn' or parsed.path not in ('/', '/index.htm'):
        return {}
    scripts = [(s, script_tokens(s.get_text())) for s in soup.find_all('script', src=False)]
    result = {}
    for title, wrapper, more_id, directory in [
            ('title-list', 'product-wrap', 'newsmore', 'xyxw'),
            ('title-list2', 'product-wrap2', 'noticemore1', 'tzgg')]:
        targets = [directory + '.htm'] + [directory + '/' + suffix + '.htm'
                    for suffix in ('dzbg', 'bksjy', 'yjsjy', 'xsgz', 'gjjl', 'xsyj')]
        branches = ''.join(('if' if i == 1 else 'else if') + f'''({i} == liindex) {{
            $('#{more_id}').attr("href","{targets[i]}"); }}''' for i in range(1, 7))
        handler = script_tokens(f'''$('.{title} li').mouseover(function(){{
            var liindex = $('.{title} li').index(this);
            {branches} else {{ $('#{more_id}').attr("href","{targets[0]}"); }}
            $(this).addClass('on').siblings().removeClass('on');
            $('.{wrapper} div.product').eq(liindex).fadeIn(150).siblings('div.product').hide();
            var liWidth = $('.{title} li').width();
            $('.case .{title} p').stop(false,true).animate({{'left' : liindex * liWidth + 'px'}},300);
        }});''')
        header = script_tokens(f"$('.{title} li').mouseover(")
        matched_scripts = [s for s, tokens in scripts if occurrences(tokens, handler) == 1]
        controls = soup.select('.' + title + ' > li')
        panels = soup.select('.' + wrapper + ' > div.product')
        more = soup.select('#' + more_id)
        valid = (len(matched_scripts) == 1 and sum(occurrences(t, header) for _, t in scripts) == 1
                 and len(controls) == len(panels) == len(targets) and len(more) == 1
                 and more[0].get('href') == targets[0]
                 and all(c.parent is controls[0].parent for c in controls)
                 and all(p.parent is panels[0].parent for p in panels)
                 and list(soup.select('.' + title + ' li')) == controls
                 and list(soup.select('.' + wrapper + ' div.product')) == panels)
        for i, panel in enumerate(panels):
            result[id(panel)] = TabBinding(panel, controls[i] if valid else None,
                more[0] if valid else None, 'official_indexed_tab_control',
                targets[i] if valid else '', matched_scripts[0] if valid else None, i if valid else -1,
                controls[0] if valid else None)
        # A shared more control has no single tab owner in static HTML. Prevent
        # generic heading inference from concatenating all seven tab labels.
        for link in more:
            result[id(link)] = TabBinding(link.parent, None)
    return result


def tab_bindings(soup, url):
    candidates = []
    for control in soup.select('[role="tab"][aria-controls]'):
        targets = control['aria-controls'].split()
        if len(targets) == 1:
            candidates.append((targets[0], control))

    parsed = urlsplit(url)
    # This official homepage defines selectSwtich by concatenating the literal
    # prefix and index. Repeated hrefLinkObj IDs are NOT the panel relationship.
    if parsed.hostname == 'www.gzhu.edu.cn' and parsed.path in ('/', '/index.htm'):
        scripts = '\n'.join(s.get_text() for s in soup.find_all('script', src=False))
        known_switch = (re.search(r'function\s+selectSwtich\s*\(', scripts) and
                        re.search(r'getElementById\(\s*showContentPrefix\s*\+\s*showContentIndex\s*\)', scripts))
        if known_switch:
            for control in soup.select('[onmouseover]'):
                match = re.fullmatch(
                    r"\s*selectSwtich\(\s*(['\"]).*?\1\s*,\s*this\s*,\s*(['\"])([\w-]+)\2\s*,\s*(\d+)\s*,\s*(['\"])[\w-]+\5\s*\)\s*;?\s*",
                    control['onmouseover'])
                if match:
                    candidates.append((match[3] + match[4], control))

    bindings = {}
    for panel_id, control in candidates:
        panels = soup.find_all(id=panel_id)
        if len(panels) != 1:
            continue
        panel = panels[0]
        if panel is control or any(p is panel for p in control.parents):
            continue
        key = id(panel)
        if key in bindings and bindings[key].control is not control:
            # Conflicting controls remain an explicit unresolved relationship.
            bindings[key] = TabBinding(panel, None)
        else:
            bindings[key] = TabBinding(panel, control)
    adapted = {**office_indexed_tabs(soup, url), **language_indexed_tabs(soup, url)}
    for key, binding in adapted.items():
        if key in bindings and bindings[key].control is not binding.control:
            bindings[key] = TabBinding(binding.panel, None)
        else:
            bindings[key] = binding
    return bindings
