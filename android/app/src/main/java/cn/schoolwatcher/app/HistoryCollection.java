package cn.schoolwatcher.app;

import java.util.List;
import java.util.function.BooleanSupplier;
import java.util.function.Consumer;

final class HistoryCollection {
    interface Fetcher {String fetch(String url) throws Exception;}
    static final class Outcome {int added,pages,failed;boolean paused,remaining;}
    static Outcome run(HistoryJournal journal,BooleanSupplier permitted,Fetcher fetch,Consumer<Integer> progress) {
        Outcome result=new Outcome();
        if(!permitted.getAsBoolean()){result.paused=true;return result;}
        journal.prepare();
        for(HistoryJournal.Item item:journal.items()) {
            while(item.pending()) {
                if(!permitted.getAsBoolean()){result.paused=true;return result;}
                if(result.pages>=150){result.remaining=true;return result;}
                try {
                    String html=fetch.fetch(item.next);
                    List<Notice> notices=NoticeParser.notices(html,item.next);
                    if(notices.isEmpty())throw new IllegalArgumentException("历史页未识别到通知，可重试或打开官网");
                    String next=NoticeParser.nextPage(html,item.next);
                    if(!permitted.getAsBoolean()){result.paused=true;return result;}
                    int added=journal.commit(item,notices,next);if(added<0)break;
                    result.added+=added;result.pages++;progress.accept(result.pages);
                }catch(Exception error) {
                    if(!permitted.getAsBoolean()){result.paused=true;return result;}
                    journal.failed(item,RefreshWorker.friendlyError(error));result.failed++;break;
                }
            }
        }
        return result;
    }
}
