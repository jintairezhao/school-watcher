package cn.schoolwatcher.app;

import java.util.Collections;
import java.util.List;
import java.util.function.BooleanSupplier;
import java.util.function.Consumer;
import java.util.function.LongSupplier;

/** A stopped request never advances the durable checkpoint. */
final class RefreshCollection {
    interface Fetcher { List<Notice> fetch(Source source) throws Exception; }
    static final class Outcome {
        final RefreshJournal.Snapshot snapshot;
        final boolean paused;
        final int added;
        Outcome(RefreshJournal.Snapshot snapshot,boolean paused,int added) {this.snapshot=snapshot;this.paused=paused;this.added=added;}
    }
    static Outcome run(RefreshJournal journal,long manualRequestedAt,LongSupplier clock,BooleanSupplier permitted,
                       Fetcher fetcher,Consumer<RefreshJournal.Snapshot> progress) {
        if(!permitted.getAsBoolean()) return new Outcome(journal.snapshot(),true,0);
        RefreshJournal.Snapshot round=journal.prepare(clock.getAsLong(),manualRequestedAt);
        int initialAdded=round.added;
        for(Source source:round.pending) {
            if(!permitted.getAsBoolean()) return new Outcome(journal.snapshot(),true,journal.snapshot().added-initialAdded);
            progress.accept(journal.snapshot());
            List<Notice> notices=Collections.emptyList();String error="";
            try {
                notices=fetcher.fetch(source);
                if(notices.isEmpty()) throw new IllegalArgumentException("没有识别到带日期的通知；请检查栏目，动态页面暂需打开官网");
            } catch(Exception failure) {error=RefreshWorker.friendlyError(failure);}
            if(!permitted.getAsBoolean()) return new Outcome(journal.snapshot(),true,journal.snapshot().added-initialAdded);
            journal.complete(round.generation,source.id,notices,error,clock.getAsLong());
        }
        RefreshJournal.Snapshot result=journal.snapshot();
        return new Outcome(result,false,result.added-initialAdded);
    }
}
