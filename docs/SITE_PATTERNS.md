# 高校官网结构调研与普适化设计

> 调研范围：24 所 985/211/双一流高校（华东/华中华南/西部/华北东北各 6 所），2026-08-30。
> 目的：总结官网结构共性，反哺抓取/发现算法的普适化设计。本文是设计依据，实现见
> `cms_profiles.yaml`、`backend/scraper/` 各模块。

## 一、CMS 格局（24 校）

| CMS | 学校 | 覆盖率 |
|---|---|---|
| **博达 VSB/JCMS/WebPlus** | 清华、南大(主)、武大、华科、湖大、中大、华工、西交(新闻)、西工大、川大(子)、电子科大、重大、兰大(子)、吉大、大工、北航、天大、南开(新闻/部分子站)、哈工大(子站) | ~19/24 |
| **苏迪 SiteEngine/SudyWP** | 复旦、浙大、南大(子)、南开(主/化学) | ~5/24 |
| **Drupal 8** | 哈工大(主站 today.hit) | 1 |
| 自建/其他 | 北大(fractal)、上交(东方怡动)、中南(Vue SPA)、浙大首页(Vue SPA) | 4 |

**结论：适配博达 + 苏迪 两大 CMS 家族即覆盖 ≈ 95% 的目标站点。**

## 二、CMS 指纹（用于自动识别）

- 博达：`/system/resource/js/`（counter.js、dynclicks.js、vsbscreen.min.js、base64.js）、
  `li[id^="line_u"]`、`span.p_pages`/`pb_sys` 分页、`dynclicks_u*` 浏览数、
  文章 URL `info/{catId}/{artId}.htm`
- 苏迪：`/_js/_portletPlugs/sudyNavi/`、`wp_` 类前缀（wp_paging/wp_news_wN）、
  `jquery.sudy.wp.visitcount.js`、列表 `/{id}/list.htm`、
  文章 `/{YYYY}/{MMDD}/c{cat}a{art}/page.htm`
- Drupal：`meta generator="Drupal 8"`、`drupalSettings`、`?page=N`（从 0 起）、
  文章 `/article/{YYYY}/{MM}/{DD}/{id}`

## 三、列表页 / 日期 / 分页共性

1. **列表 DOM**：绝大多数为 `li > a(标题) + span/div(日期)`；博达变体是日期与标题
   拼在同一 `<a>` 文本内（清华 `DD YYYY.MM标题`、南大 `MM-DD YYYY标题`、
   北航 `日b + 年月span`），需正则拆分而非独立日期节点。
2. **日期格式**：ISO 横线、点分、斜线、中文年月日、年月+日分离、日+年月分离、
   英文月、相对时间（Drupal「N小时前」）八类并存。
3. **分页**三种静态模式：博达 `{栏目}/{N}.htm` 降序（p_pages/pb_sys）、
   苏迪 `{栏目}/list{N}.htm`、Drupal `?page=N` 零起。均无 AJAX（例外：北大
   notices.html 走 `/dat/recentList.xml` AJAX）。
4. **子站**：普遍同域子域名（cs.xxx.edu.cn），CMS 与主站同构（南开等少数跨 CMS），
   解析器按 CMS 指纹复用而非按学校。

## 四、障碍与对策

| 障碍 | 表现 | 对策（已实现/待实现） |
|---|---|---|
| 瑞数 WAF | 川大主站 202+`$_ts`、兰大 412 | HTTP 202/412 或 `$_ts` 壳页 → Playwright 回退（本次实现） |
| SPA 首页 | 浙大/中南首页 Vue 空壳 | 首页走浏览器回退；列表页仍 SSR 可直取 |
| 需登录通知 | 北大 portal、中南 OA、华科 one | 不纳入公开抓取，发现阶段跳过 |
| 微信外链 | 天大/大工 tzgg 混入 mp.weixin.qq.com | 条目级跳过（本次实现） |
| 日期/标题拼接 | 博达多校 | 日期正则拆分 + 标题质量门控（已有） |

## 五、普适化设计落地

1. **数据驱动 CMS 模板**（`cms_profiles.yaml`）：按 CMS 家族追加 selector_profiles
   （博达 line_u / info 链接型、苏迪 list.htm 型、Drupal 型、北大/上交自建型），
   探测时按「指纹识别 → 家族模板优先 → DOM 分析兜底」顺序匹配。
2. **日期注册表**扩充：日+年月（`26 2026.08`）、年.月-日（`2026.07-22`）、
   相对时间（`N小时前/天前`）。
3. **翻页注册表**扩充：`list{N}.htm`、`?page=N` 零偏移（Drupal）。
4. **抓取层**：202/412/`$_ts` → 浏览器回退；微信链接条目跳过。
5. **发现层**：通知入口启发式加入博达/苏迪常见路径
   （`tzgg.htm`、`xwtz/`、`xxgg/`、`/{id}/list.htm`）与标签（校园公告/信息公告/公告）。
6. **精度层**（已有，全校生效）：标题质量门控 `title_quality.py`，
   保证任何 CMS 下垃圾标题不入库。

**原则：识别靠指纹不靠校名；新增 CMS 只改 YAML 不改代码。**

## 六、已知局限（诚实记录）

1. **博达降序分页**：`{栏目}/{N}.htm` 的 N 是倒序文件号，引擎按升序近似映射，
   深历史翻页不完整；近期内容（SINCE_YEAR 窗口内）不受影响。
2. **需登录源不抓**：北大 portal、中南 OA、华科 one 等 CAS/门户通知不纳入公开抓取。
3. **瑞数 WAF 主站**（川大/兰大）走 Playwright 回退，成本高、速度受限；其无 WAF 子站可直取。
4. 验证基线：12 个调研校列表页 12/12 探测命中、垃圾标题 0（2026-08-30）。
