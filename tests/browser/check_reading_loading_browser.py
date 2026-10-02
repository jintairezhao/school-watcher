"""Real templates/navigation, controlled slow I/O: no external or paid requests."""
import json, logging, sys, tempfile, threading
from datetime import datetime, timedelta
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from backend import create_app
from backend.database.db import db
from backend.database.models import User,School,Department,Subscription,Announcement
from playwright.sync_api import sync_playwright,expect
from werkzeug.serving import make_server

def check():
 logging.getLogger('werkzeug').setLevel(logging.ERROR)
 out=ROOT/'.local/reading-20261002';out.mkdir(parents=True,exist_ok=True)
 with tempfile.TemporaryDirectory(prefix='watcher-reading-') as scratch:
  app=create_app({'TESTING':True,'SECRET_KEY':'reading-test','SQLALCHEMY_DATABASE_URI':'sqlite:///'+str(Path(scratch)/'db.sqlite'),
                  'SOURCE_CATALOG_PATH':str(Path(scratch)/'catalog.sqlite')})
  with app.app_context():
   db.create_all();user=User(username='阅读测试',password_hash='unused',role='admin');school=School(name='示例大学',url='https://example.edu.cn/',subscriber_count=1)
   db.session.add_all([user,school]);db.session.flush()
   for i in range(1,4):db.session.add(Department(id=i,school_id=school.id,name=f'通知栏目{i}',list_url=f'https://example.edu.cn/{i}/'))
   db.session.flush();db.session.add(Subscription(user_id=user.id,school_id=school.id));now=datetime.utcnow()
   for i in range(1,31):db.session.add(Announcement(id=i,school_id=school.id,department_id=1,title=f'关于第{i}项学习活动的通知',url=f'https://example.edu.cn/notice/{i}',published_at=now-timedelta(minutes=i),
                   content_html=None if i==1 else '<p>这是已保存的正文。</p>',content_cached_at=None if i==1 else now))
   db.session.commit();cookie=app.session_interface.get_signing_serializer(app).dumps({'user_id':user.id,'_csrf_token':'reading'})
  server=make_server('127.0.0.1',0,app,threaded=True);threading.Thread(target=server.serve_forever,daemon=True).start();base=f'http://127.0.0.1:{server.server_port}'
  try:
   with sync_playwright() as p:
    browser=p.chromium.launch(channel='msedge',headless=True)
    for width,theme in ((1440,'dark'),(390,'light')):
     context=browser.new_context(viewport={'width':width,'height':900},color_scheme=theme,reduced_motion='no-preference' if width==1440 else 'reduce')
     context.add_cookies([{'name':'session','value':cookie,'url':base}]);context.add_init_script(f"localStorage.setItem('theme','{theme}')")
     page=context.new_page();page.set_default_timeout(10000);errors=[];fragments=[];body_mode=['pending'];body_posts=[];version=[0]
     page.on('pageerror',lambda error:errors.append(str(error)))
     page.on('request',lambda request:fragments.append(request.url) if request.headers.get('x-inbox-fragment')=='1' else None)
     def route(request):
      url=request.request.url
      if '/api/inbox/sync' in url or '/api/inbox/refresh' in url:
       sources=[{'id':i,'name':f'栏目{i}','state':'done' if i<=1+version[0] else 'running','new_count':version[0] if i==2 else 0,
                 'updated_at':f'2026-10-02T00:00:0{version[0]}','pages_checked':1} for i in range(1,4)]
       request.fulfill(json={'total':3,'done':1+version[0],'failed':0,'active':2-version[0],'new_count':version[0],'tracked':True,'scheduled':0,
                            'tracking':{'scope':'all','scope_label':'全部订阅'},'sources':sources});return
      if '/api/announcements/' in url and url.endswith('/content'):
       if request.request.method=='POST':body_posts.append(url)
       if body_mode[0]=='network':request.abort();return
       if body_mode[0]=='failed':request.fulfill(json={'content_status':'failed','error':'官网要求登录后才能读取正文。'});return
       if body_mode[0]=='saved':request.fulfill(json={'content_status':'saved','content_html':'<p>可以选中并保持位置的通知正文。</p>'});return
       request.fulfill(json={'content_status':'pending'});return
      if not url.startswith(base):request.abort();return
      request.continue_()
     context.route('**/*',route)
     page.goto(base+'/?school=1&period=all&selected=1')
     expect(page.locator('.body-loader-placeholder')).to_be_visible()
     expect(page.locator('.body-loader-content')).to_have_attribute('aria-busy','true')
     assert page.locator('.body-loader-retry').is_hidden()
     if width==1440:
      expect(page.locator('#inboxRefreshProgress')).to_have_attribute('aria-valuenow','1')
      expect(page.locator('#inboxRefreshProgress')).to_have_attribute('aria-valuemax','3')
      assert page.locator('#inboxRefreshProgress').evaluate('(el)=>Math.abs(el.clientWidth-el.clientHeight)<1')
     animation=page.locator('.skeleton-line').first.evaluate('(el)=>getComputedStyle(el,"::after").animationName')
     assert animation==('body-shimmer' if width==1440 else 'none'),animation
     page.screenshot(path=str(out/f'loading-{width}.png'),full_page=True)
     body_mode[0]='failed'
     expect(page.locator('.body-loader-status')).to_contain_text('官网要求登录')
     expect(page.locator('.body-loader-placeholder')).to_be_hidden()
     expect(page.locator('.body-loader-content')).to_have_attribute('aria-busy','false')
     expect(page.locator('.body-loader-retry')).to_be_enabled()
     page.screenshot(path=str(out/f'failure-{width}.png'),full_page=True)
     body_mode[0]='network';page.locator('.body-loader-retry').click()
     expect(page.locator('.body-loader-status')).to_contain_text('连接中断')
     expect(page.locator('.body-loader-placeholder')).to_be_hidden()
     body_mode[0]='saved';page.locator('.body-loader-retry').click()
     expect(page.locator('.body-loader-content')).to_contain_text('可以选中并保持位置')
     expect(page.locator('.body-loader-placeholder')).to_be_hidden()
     assert len(body_posts)==3
     if width==390:
      page.locator('.reader-back').click();expect(page.locator('#noticeList')).to_be_visible()
      page.wait_for_function('() => !document.querySelector(".notice-panel").hasAttribute("aria-busy") && !new URL(location.href).searchParams.has("selected")')
     page.evaluate('''() => {const list=document.querySelector('#noticeList'); list.style.scrollBehavior='auto'; list.scrollTop=260;
       window.savedList=list;window.savedRow=list.querySelectorAll('[data-notice-link]')[4];window.savedScroll=list.scrollTop;
       window.savedRowTop=savedRow.getBoundingClientRect().top; window.savedReader=document.querySelector('.reader-panel');
       const text=document.querySelector('.body-loader-content p')?.firstChild;
       if(text){const r=document.createRange();r.selectNodeContents(text);getSelection().removeAllRanges();getSelection().addRange(r);}
       window.savedSelection=String(getSelection());}''')
     before=len(fragments)
     with app.app_context():
      ann=db.session.get(Announcement,100)
      if not ann:
       db.session.add(Announcement(id=100,school_id=1,department_id=1,title='刚采集的新通知',url='https://example.edu.cn/new',published_at=datetime.utcnow()));db.session.commit()
     version[0]=1
     expect(page.locator('#inboxNewNotices')).to_be_visible()
     assert len(fragments)==before,'Progress polling replaced the reading panels'
     assert page.evaluate('savedList===document.querySelector("#noticeList") && savedReader===document.querySelector(".reader-panel")')
     positions=page.evaluate('({savedScroll,now:savedList.scrollTop,savedRowTop,top:savedRow.getBoundingClientRect().top})')
     assert abs(positions['savedScroll']-positions['now'])<1 and abs(positions['savedRowTop']-positions['top'])<1,positions
     assert page.evaluate('String(getSelection())===savedSelection')
     page.screenshot(path=str(out/f'stable-list-{width}.png'),full_page=True)
     # Selecting another card must not apply the arriving list or replace its nodes.
     page.locator('[data-notice-link][data-announcement-id="5"]').click()
     expect(page.locator('[data-content-id="5"]')).to_be_attached()
     assert page.evaluate('savedList===document.querySelector("#noticeList") && savedRow.isConnected')
     if width==390:
      page.locator('.reader-back').click()
      page.wait_for_function('() => !document.querySelector(".notice-panel").hasAttribute("aria-busy") && !new URL(location.href).searchParams.has("selected")')
     page.locator('#inboxNewNotices').click()
     expect(page.locator('[data-notice-link][data-announcement-id="100"]')).to_be_attached()
     expect(page.locator('#inboxNewNotices')).to_be_hidden()
     assert not errors,errors
     assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
     context.close()
     # Keep the next viewport's initial list identical.
     with app.app_context():db.session.delete(db.session.get(Announcement,100));db.session.commit()
    browser.close()
   print(json.dumps({'passed':True,'widths':[1440,390],'checks':['circular actual progress','skeleton and shimmer','reduced motion','login failure','network failure','retry success','no background DOM replacement','scroll and text selection stable','article switch preserves cards','explicit apply new notices']}))
  finally:
   server.shutdown();server.server_close()
   with app.app_context():db.session.remove();db.engine.dispose()
if __name__=='__main__':check()
