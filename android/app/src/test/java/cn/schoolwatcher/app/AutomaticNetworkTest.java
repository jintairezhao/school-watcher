package cn.schoolwatcher.app;

import android.app.Application;
import android.net.NetworkCapabilities;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.robolectric.RobolectricTestRunner;
import org.robolectric.annotation.Config;
import static org.junit.Assert.*;
import static org.robolectric.Shadows.shadowOf;

@RunWith(RobolectricTestRunner.class)
@Config(sdk=28,application=Application.class)
public class AutomaticNetworkTest {
    private NetworkCapabilities network(int transport) {
        NetworkCapabilities result=new NetworkCapabilities();
        shadowOf(result).addTransportType(transport);
        shadowOf(result).addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET);
        return result;
    }
    @Test public void meteredWifiStillCountsAsWifiWithoutExternalValidation() {
        NetworkCapabilities wifi=network(NetworkCapabilities.TRANSPORT_WIFI);
        shadowOf(wifi).removeCapability(NetworkCapabilities.NET_CAPABILITY_NOT_METERED);
        assertTrue(AutomaticNetwork.accepts(true,wifi));
        assertTrue(AutomaticNetwork.accepts(false,wifi));
    }
    @Test public void unmeteredMobileDataIsBlockedUntilUserTurnsWifiOnlyOff() {
        NetworkCapabilities mobile=network(NetworkCapabilities.TRANSPORT_CELLULAR);
        shadowOf(mobile).addCapability(NetworkCapabilities.NET_CAPABILITY_NOT_METERED);
        assertFalse(AutomaticNetwork.accepts(true,mobile));
        assertTrue(AutomaticNetwork.accepts(false,mobile));
    }
    @Test public void disconnectedAndLocalOnlyNetworksCannotStartAutomaticCollection() {
        assertFalse(AutomaticNetwork.accepts(false,null));
        assertFalse(AutomaticNetwork.accepts(true,null));
        NetworkCapabilities local=network(NetworkCapabilities.TRANSPORT_WIFI);
        shadowOf(local).removeCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET);
        assertFalse(AutomaticNetwork.accepts(false,local));
    }
    @Test public void mixedVpnCannotSpendMobileDataInWifiOnlyMode() {
        NetworkCapabilities vpn=network(NetworkCapabilities.TRANSPORT_WIFI);
        shadowOf(vpn).addTransportType(NetworkCapabilities.TRANSPORT_VPN);
        shadowOf(vpn).addTransportType(NetworkCapabilities.TRANSPORT_CELLULAR);
        assertFalse(AutomaticNetwork.accepts(true,vpn));
        assertTrue(AutomaticNetwork.accepts(false,vpn));
    }
}
