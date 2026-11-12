package cn.schoolwatcher.app;

import android.app.Application;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import androidx.appcompat.app.AppCompatDelegate;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public final class WatcherApplication extends Application {
    public static final ExecutorService IO = Executors.newFixedThreadPool(3);
    private WatcherStore store;
    private volatile boolean visible;
    private int startedActivities;
    @Override public void onCreate() {
        super.onCreate(); store = new WatcherStore(this);
        AppCompatDelegate.setDefaultNightMode(getSharedPreferences("settings",0).getInt("theme",AppCompatDelegate.MODE_NIGHT_FOLLOW_SYSTEM));
        NotificationChannel channel = new NotificationChannel("notices","新学校通知",NotificationManager.IMPORTANCE_DEFAULT);
        channel.setDescription("订阅栏目发现新通知时提醒");
        getSystemService(NotificationManager.class).createNotificationChannel(channel);
        registerActivityLifecycleCallbacks(new ActivityLifecycleCallbacks() {
            public void onActivityStarted(android.app.Activity activity) {startedActivities++;visible=true;}
            public void onActivityStopped(android.app.Activity activity) {startedActivities--;visible=startedActivities>0 || activity.isChangingConfigurations();}
            public void onActivityCreated(android.app.Activity activity,android.os.Bundle state) {}
            public void onActivityResumed(android.app.Activity activity) {}
            public void onActivityPaused(android.app.Activity activity) {}
            public void onActivitySaveInstanceState(android.app.Activity activity,android.os.Bundle state) {}
            public void onActivityDestroyed(android.app.Activity activity) {}
        });
        RefreshWorker.configure(this);
    }
    public WatcherStore store() { return store; }
    public boolean isVisible() { return visible; }
}
