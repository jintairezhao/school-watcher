package cn.schoolwatcher.app;

import android.app.ActivityManager;
import android.content.Context;
import android.os.Build;
import android.os.PowerManager;

final class BackgroundAccess {
    static boolean exempt(Context context) {
        PowerManager power=context.getSystemService(PowerManager.class);
        return power!=null && power.isIgnoringBatteryOptimizations(context.getPackageName());
    }
    static boolean restricted(Context context) {
        ActivityManager activity=context.getSystemService(ActivityManager.class);
        return Build.VERSION.SDK_INT>=28 && activity!=null && activity.isBackgroundRestricted();
    }
    static boolean allowed(Context context) {
        return context.getSharedPreferences("settings",0).getBoolean("background",false) && exempt(context) && !restricted(context);
    }
    static String status(Context context) {
        if(!context.getSharedPreferences("settings",0).getBoolean("background",false)) return "后台刷新已关闭，仅在打开应用时检查。";
        if(restricted(context)) return "系统已限制后台运行，当前仅在打开应用时检查。";
        if(!exempt(context)) return "等待系统授权：请在电池优化设置中允许「学校通知」不受限制。授权前仅在打开应用时检查。";
        return "系统电池优化已豁免，将按网络与电量条件尝试后台刷新。厂商的额外限制仍以手机设置为准。";
    }
}
