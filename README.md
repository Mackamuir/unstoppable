# unstoppable
Unstoppable is a mod for deadlock that preserves critical files.
This repository is not the mod, rather the tools used to create the mod.

It also automatically uploads the created zip to GameBanana, so if that intrests you check out /src/publisher.py.
Not sure if it will be as easy as just updating your mod ID's, maybe it is. idk.

If building for yourself I suggest you create a steam account specifically for this container that owns Deadlock.
If using an account with 2FA you will need to exec into the container and login to DepotDownloader.
```bash
docker exec -it unstoppable /opt/depotdownloader/DepotDownloader -app 1422450 -username <username> -password <password> -remember-password
```
See https://github.com/SteamRE/DepotDownloader for more details
