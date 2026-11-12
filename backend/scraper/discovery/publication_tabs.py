"""Bind a publication panel to its explicit control; never infer tab order."""
import re
import hashlib
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
    evidence: object = None


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


def student_indexed_tabs(soup, url, html):
    """Use the retained literal handler, with its local DOM traversals checked."""
    parsed = urlsplit(url)
    if parsed.hostname != 'www.cupk.edu.cn' or parsed.path != '/xszz/':
        return {}
    from .script_materials import verified_scripts
    handler = script_tokens('''$(".c-zizhu-notice-module-detail__tab-ul li").click(function() {
        var curIndex = $(this).index();
        $(this).parents().siblings(".c-zizhu-module__head-more").children(".c-zizhu-module__body-a").hide().eq(curIndex).show();
        $(this).parents().children("li").eq(curIndex).addClass("cur").siblings().removeClass('cur');
        $(this).parents().parents().siblings(".c-zizhu-notice-module-detail__tab-div").hide().eq(curIndex).show();
    });''')
    header = script_tokens('$(".c-zizhu-notice-module-detail__tab-ul li").click(')
    scripts = [(script_tokens(text), proof) for _, text, proof in verified_scripts(html, url, soup)]
    scripts += [(script_tokens(s.get_text()), None) for s in soup.find_all('script', src=False)]
    matched = [proof for tokens, proof in scripts if occurrences(tokens, handler) == 1]
    valid_script = (len(matched) == 1 and matched[0] is not None
                    and sum(occurrences(tokens, header) for tokens, _ in scripts) == 1)
    result = {id(panel): TabBinding(panel, None) for panel in
              soup.select('.c-zizhu-notice-module-detail__tab-div')}
    for group in soup.select('.c-zizhu-notice-module-detail__tab-ul'):
        head, module = group.parent, group.parent.parent
        controls = group.find_all('li', recursive=False)
        panels = module.select(':scope > .c-zizhu-notice-module-detail__tab-div')
        more = head.select(':scope > .c-zizhu-module__head-more > .c-zizhu-module__body-a')
        # The exact handler traverses these sibling collections. Extra nested
        # controls, heads or panels would change its index domain.
        valid = (valid_script and 'c-zizhu-module__head' in head.get('class', [])
                 and len(module.select(':scope > .c-zizhu-module__head')) == 1
                 and len(head.select('.c-zizhu-notice-module-detail__tab-ul')) == 1
                 and len(module.select('.c-zizhu-notice-module-detail__tab-div')) == len(panels)
                 and len(controls) == len(panels) == len(more) > 1
                 and group.find_all('li') == controls
                 and group.find_all(recursive=False) == controls
                 and len(head.select('.c-zizhu-module__head-more')) == 1
                 and head.select('.c-zizhu-module__body-a') == more)
        for index, panel in enumerate(panels):
            proof = dict(matched[0], method='retained_external_indexed_handler', tab_index=index,
                         handler_hash=hashlib.sha256(' '.join(handler).encode()).hexdigest()) if valid else None
            binding = TabBinding(panel, controls[index] if valid else None, more[index] if valid else None,
                method='official_indexed_tab_control', tab_index=index if valid else -1, evidence=proof)
            result[id(panel)] = binding
            if valid:
                result[id(more[index])] = binding
        if not valid:
            for link in more:
                result[id(link)] = TabBinding(module, None)
    return result


def tab_bindings(soup, url, html=''):
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
    adapted = {**office_indexed_tabs(soup, url), **language_indexed_tabs(soup, url),
               **student_indexed_tabs(soup, url, html)}
    for key, binding in adapted.items():
        if key in bindings and bindings[key].control is not binding.control:
            bindings[key] = TabBinding(binding.panel, None)
        else:
            bindings[key] = binding
    return bindings
