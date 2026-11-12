package cn.schoolwatcher.app;

import android.content.Context;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;

/** Wi-Fi is a transport choice, not a synonym for an unmetered connection. */
final class AutomaticNetwork {
    static boolean accepts(boolean wifiOnly,NetworkCapabilities capabilities) {
        if(capabilities==null || !capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)) return false;
        return !wifiOnly || (capabilities.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)
                && !capabilities.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR));
    }
    static boolean allowed(Context context,Network network) {
        ConnectivityManager manager=context.getSystemService(ConnectivityManager.class);
        return manager!=null && network!=null && accepts(context.getSharedPreferences("settings",0).getBoolean("wifi",true),manager.getNetworkCapabilities(network));
    }
    static Network selected(Context context) {
        ConnectivityManager manager=context.getSystemService(ConnectivityManager.class);
        Network network=manager==null?null:manager.getActiveNetwork();
        return allowed(context,network)?network:null;
    }
    static boolean allowed(Context context) {return selected(context)!=null;}
    static String waitingMessage(Context context) {
        return context.getSharedPreferences("settings",0).getBoolean("wifi",true)
                ?"自动抓取等待 Wi-Fi；也可点击刷新使用当前网络":"自动抓取等待网络连接";
    }
}
