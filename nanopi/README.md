# NanoPi — Configuration dbus-mqtt-battery

Fichiers à copier sur le NanoPi Venus OS pour les deux instances du driver
[dbus-mqtt-battery](https://github.com/mr-manuel/venus-os_dbus-mqtt-battery).

## Structure sur le NanoPi

```
/data/etc/
  dbus-mqtt-battery-41/
    config.ini          ← copier le contenu de config-bms1.ini
  dbus-mqtt-battery-42/
    config.ini          ← copier le contenu de config-bms2.ini
```

## Déploiement

```bash
# Depuis le RPi CM5 (adapter l'IP du NanoPi)
scp nanopi/config-bms1.ini root@192.168.1.120:/data/etc/dbus-mqtt-battery-41/config.ini
scp nanopi/config-bms2.ini root@192.168.1.120:/data/etc/dbus-mqtt-battery-42/config.ini

# Redémarrer les drivers sur le NanoPi
ssh root@192.168.1.120 "svc -t /service/dbus-mqtt-battery-41 /service/dbus-mqtt-battery-42"
```

## Topic MQTT publié par le RPi CM5

```
santuario/bms/1/venus   → dbus-mqtt-battery-41  (Pack 320Ah)
santuario/bms/2/venus   → dbus-mqtt-battery-42  (Pack 360Ah)
```

Activé par `MQTT_VENUS_ENABLED=1` dans le `.env` Python du RPi CM5.

## Vérification sur le NanoPi

```bash
# Vérifier que le driver est actif
svstat /service/dbus-mqtt-battery-41

# Voir les données reçues sur D-Bus
dbus -y com.victronenergy.battery.mqtt_battery_141 / GetValue

# Logs du driver
tail -f /var/log/dbus-mqtt-battery-41/current
```
