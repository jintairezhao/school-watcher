package cn.schoolwatcher.app;

import android.app.ActivityManager;
import android.app.Application;
import android.os.PowerManager;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.RuntimeEnvironment;
import org.robolectric.Shadows;
import org.robolectric.annotation.Config;
import static org.junit.Assert.*;

@RunWith(RobolectricTestRunner.class)
@Config(sdk=28,application=Application.class)
public class BackgroundAccessTest {
    @Test public void appSwitchAndSystemAuthorizationMustBothRemainEnabled() {
        Application app=RuntimeEnvironment.getApplication();
        app.getSharedPreferences("settings",0).edit().putBoolean("background",true).commit();
        assertFalse(BackgroundAccess.allowed(app));
        Shadows.shadowOf(app.getSystemService(PowerManager.class)).setIgnoringBatteryOptimizations(app.getPackageName(),true);
        assertTrue(BackgroundAccess.allowed(app));
        Shadows.shadowOf(app.getSystemService(ActivityManager.class)).setBackgroundRestricted(true);
        assertFalse(BackgroundAccess.allowed(app));
        Shadows.shadowOf(app.getSystemService(ActivityManager.class)).setBackgroundRestricted(false);
        Shadows.shadowOf(app.getSystemService(PowerManager.class)).setIgnoringBatteryOptimizations(app.getPackageName(),false);
        assertFalse(BackgroundAccess.allowed(app));
        Shadows.shadowOf(app.getSystemService(PowerManager.class)).setIgnoringBatteryOptimizations(app.getPackageName(),true);
        app.getSharedPreferences("settings",0).edit().putBoolean("background",false).commit();
        assertFalse(BackgroundAccess.allowed(app));
    }
}
