import asyncio
import datetime
import hashlib
import html
import json
import random
import urllib.parse
import urllib.request

import requests

import pymysql
import semver
import server
import server.db as db

from . import config
from .config import FAF_POLICY_SERVER_BASE_URL
from .connectivity import Connectivity, ConnectivityState
from .decorators import timed, with_logger
from .game_service import GameService
from .gameconnection import GameConnection
from .games import GameMode, GameState, VisibilityState
from .geoip_service import GeoIpService
from .matchmaker import MatchmakerQueue, Search
from .player_service import PlayerService
from .players import Player, PlayerState
from .protocol import QDataStreamProtocol
from .types import Address


class ClientError(Exception):
    """
    Represents a ClientError

    If recoverable is False, it is expected that the
    connection be terminated immediately.
    """
    def __init__(self, message, recoverable=True, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.message = message
        self.recoverable = recoverable


class AuthenticationError(Exception):
    def __init__(self, message, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.message = message


@with_logger
class LobbyConnection():
    @timed()
    def __init__(
        self,
        games: GameService,
        players: PlayerService,
        geoip: GeoIpService,
        matchmaker_queue: MatchmakerQueue
    ):
        self.geoip_service = geoip
        self.game_service = games
        self.player_service = players
        self.matchmaker_queue = matchmaker_queue
        self.ladderPotentialPlayers = []
        self.warned = False
        self._authenticated = False
        self.player = None  # type: Player
        self.game_connection = None  # type: GameConnection
        self.connectivity = None  # type: Connectivity
        self.leagueAvatar = None
        self.peer_address = None  # type: Optional[Address]
        self.session = int(random.randrange(0, 4294967295))
        self.protocol = None
        self._logger.debug("LobbyConnection initialized")
        self.search = None
        self.user_agent = None

    @property
    def authenticated(self):
        return self._authenticated

    @asyncio.coroutine
    def on_connection_made(self, protocol: QDataStreamProtocol, peername: Address):
        self.protocol = protocol
        self.peer_address = peername
        server.stats.incr("server.connections")

    def abort(self, logspam=""):
        if self.player:
            self._logger.warning("Client %s dropped. %s" % (self.player.login, logspam))
        else:
            self._logger.warning("Aborting %s. %s" % (self.peer_address.host, logspam))
        if self.game_connection:
            self.game_connection.abort()
            self.game_connection = None
        self._authenticated = False
        self.protocol.writer.close()

    def ensure_authenticated(self, cmd):
        if not self._authenticated:
            if cmd not in ['hello', 'ask_session', 'create_account', 'ping', 'pong']:
                self.abort("Message invalid for unauthenticated connection: %s" % cmd)
                return False
        return True

    async def on_message_received(self, message):
        """
        Dispatches incoming messages
        """
        try:
            cmd = message['command']
            if not self.ensure_authenticated(cmd):
                return
            target = message.get('target')
            if target == 'game':
                if not self.game_connection:
                    return
                await self.game_connection.handle_action(cmd, message.get('args', []))
                return
            elif target == 'connectivity':
                if not self.connectivity:
                    return
                await self.connectivity.on_message_received(message)
                return
            handler = getattr(self, 'command_{}'.format(cmd))
            if asyncio.iscoroutinefunction(handler):
                await handler(message)
            else:
                handler(message)
        except AuthenticationError as ex:
            self.protocol.send_message(
                {'command': 'authentication_failed',
                 'text': ex.message}
            )
        except ClientError as ex:
            self.protocol.send_message(
                {'command': 'notice',
                 'style': 'error',
                 'text': ex.message}
            )
            if not ex.recoverable:
                self.abort(ex.message)
        except (KeyError, ValueError) as ex:
            self._logger.exception(ex)
            self.abort("Garbage command: {}".format(message))
        except Exception as ex:  # pragma: no cover
            self.protocol.send_message({'command': 'invalid'})
            self._logger.exception(ex)
            self.abort("Error processing command")

    def command_ping(self, msg):
        self.protocol.send_raw(self.protocol.pack_message('PONG'))

    def command_pong(self, msg):
        pass

    @asyncio.coroutine
    def command_create_account(self, message):
        raise ClientError("FAF no longer supports direct registration. Please use the website to register.", recoverable=True)

    @timed()
    async def send_tutorial_section(self):
        reply = []

        async with db.engine.acquire() as conn:
            # Can probably replace two queries with one here if we're smart enough.
            result = await conn.execute("SELECT `section`,`description` FROM `tutorial_sections`")

            async for row in result:
                section, description = row[0], row[1]
                reply.append({
                    "command": "tutorials_info",
                    "section": section,
                    "description": description}
                )

            result = await conn.execute(
                """ SELECT tutorial_sections.`section`, `name`, `url`, `tutorials`.`description`, `map`
                    FROM `tutorials`
                    LEFT JOIN tutorial_sections ON tutorial_sections.id = tutorials.section
                    ORDER BY `tutorials`.`section`, name"""
            )

            async for row in result:
                section, tutorial_name, url, description, map_name = row[0], row[1], row[2], row[3], row[4]
                reply.append({"command": "tutorials_info", "tutorial": tutorial_name, "url": url,
                              "tutorial_section": section, "description": description,
                              "mapname": map_name})

        self.protocol.send_messages(reply)

    async def send_coop_maps(self):
        async with db.engine.acquire() as conn:
            result = await conn.execute("SELECT name, description, filename, type, id FROM `coop_map`")

            maps = []
            async for row in result:
                json_to_send = {
                    "command": "coop_info",
                    "name": row["name"],
                    "description": row["description"],
                    "filename": row["filename"],
                    "featured_mod": "coop"
                }
                campaigns = [
                    "FA Campaign",
                    "Aeon Vanilla Campaign",
                    "Cybran Vanilla Campaign",
                    "UEF Vanilla Campaign",
                    "Custom Missions"
                ]
                if row["type"] < len(campaigns):
                    json_to_send["type"] = campaigns[row["type"]]
                else:
                    # Don't sent corrupt data to the client...
                    self._logger.error("Unknown coop type!")
                    continue
                json_to_send["uid"] = row["id"]
                maps.append(json_to_send)

        self.protocol.send_messages(maps)

    @timed
    def send_mod_list(self):
        self.protocol.send_messages(self.game_service.all_game_modes())

    @timed()
    def send_game_list(self):
        self.sendJSON({
            'command': 'game_info',
            'games': [game.to_dict() for game in self.game_service.open_games]
        })

    async def command_social_remove(self, message):
        if "friend" in message:
            target_id = message['friend']
        elif "foe" in message:
            target_id = message['foe']
        else:
            self.abort("No-op social_remove.")
            return

        async with db.engine.acquire() as conn:
            await conn.execute(
                "DELETE FROM friends_and_foes WHERE user_id = %s AND subject_id = %s",
                (self.player.id, target_id))

    @timed()
    async def command_social_add(self, message):
        if "friend" in message:
            status = "FRIEND"
            target_id = message['friend']
        elif "foe" in message:
            status = "FOE"
            target_id = message['foe']
        else:
            return

        async with db.engine.acquire() as conn:
            await conn.execute(
                "INSERT INTO friends_and_foes(user_id, subject_id, `status`) VALUES(%s, %s, %s)",
                (self.player.id, target_id, status))

    def kick(self, message=None):
        self.sendJSON(dict(command="notice", style="kick"))
        if message:
            self.sendJSON(dict(command="notice", style="info",
                                                  text=message))
        self.abort()

    def send_updated_achievements(self, updated_achievements):
        self.sendJSON(dict(command="updated_achievements", updated_achievements=updated_achievements))

    async def command_admin(self, message):
        action = message['action']

        if self.player.admin:
            if action == "closeFA":
                player = self.player_service[message['user_id']]
                if player:
                    self._logger.warn('Administrative action: %s closed game for %s', self.player, player)
                    player.lobby_connection.sendJSON(dict(command="notice", style="kill"))
                    player.lobby_connection.sendJSON(dict(command="notice", style="info",
                                       text=("Your game was closed by an administrator ({admin_name}). "
                                             "Please refer to our rules for the lobby/game here {rule_link}."
                                       .format(admin_name=self.player.login,
                                               rule_link=config.RULE_LINK))))

            elif action == "closelobby":
                player = self.player_service[message['user_id']]
                ban_fail = None
                if player:
                    if 'ban' in message:
                        reason = message['ban'].get('reason', 'Unspecified')
                        duration = int(message['ban'].get('duration', 1))
                        period = message['ban'].get('period', 'DAY')
                        self._logger.warn('Administrative action: %s closed client for %s with %s ban (Reason: %s)', self.player, player, duration, reason)
                        async with db.engine.acquire() as conn:
                            try:
                                result = await conn.execute("SELECT reason from lobby_ban WHERE idUser=%s AND expires_at > NOW()", (message['user_id']))

                                row = await result.fetchone()
                                if row:
                                    ban_fail = row[0]
                                else:
                                    # FIXME: Interpolating the period into this is terrible and insecure - but the data comes from trusted users (admins) only
                                    await conn.execute(
                                        "INSERT INTO ban (player_id, author_id, reason, expires_at, level) VALUES (%s, %s, %s, DATE_ADD(NOW(), INTERVAL %s {}), 'GLOBAL')".format(period),
                                        (player.id, self.player.id, reason, duration))
                            except pymysql.MySQLError as e:
                                raise ClientError('Your ban attempt upset the database: {}'.format(e))
                    else:
                        self._logger.warn('Administrative action: %s closed client for %s', self.player, player)
                    player.lobby_connection.kick(
                        message=("You were kicked from FAF by an administrator ({admin_name}). "
                         "Please refer to our rules for the lobby/game here {rule_link}."
                          .format(admin_name=self.player.login,
                                  rule_link=config.RULE_LINK)))
                    if ban_fail:
                        raise ClientError("Kicked the player, but he was already banned!")

            elif action == "requestavatars":
                async with db.engine.acquire() as conn:
                    result = await conn.execute("SELECT url, tooltip FROM `avatars_list`")

                    data = {"command": "admin", "avatarlist": []}
                    async for row in result:
                        data['avatarlist'].append({
                            "url": row["url"],
                            "tooltip": row["tooltip"]
                        })

                    self.sendJSON(data)

            elif action == "remove_avatar":
                idavatar = message["idavatar"]
                iduser = message["iduser"]
                async with db.engine.acquire() as conn:
                    await conn.execute("DELETE FROM `avatars` "
                                              "WHERE `idUser` = %s "
                                              "AND `idAvatar` = %s", (iduser, idavatar))

            elif action == "add_avatar":
                who = message['user']
                avatar = message['avatar']

                async with db.engine.acquire() as conn:
                    if avatar is None:
                        await conn.execute(
                            "DELETE FROM `avatars` "
                            "WHERE `idUser` = "
                            "(SELECT `id` FROM `login` WHERE `login`.`login` = %s)", (who, ))
                    else:
                        await conn.execute(
                            "INSERT INTO `avatars`(`idUser`, `idAvatar`) "
                            "VALUES ((SELECT id FROM login WHERE login.login = %s),"
                            "(SELECT id FROM avatars_list WHERE avatars_list.url = %s)) "
                            "ON DUPLICATE KEY UPDATE `idAvatar` = (SELECT id FROM avatars_list WHERE avatars_list.url = %s)",
                            (who, avatar, avatar))

            elif action == "broadcast":
                for player in self.player_service:
                    try:
                        if player.lobby_connection:
                            player.lobby_connection.send_warning(message.get('message'))
                    except Exception as ex:
                        self._logger.debug("Could not send broadcast message to %s: %s".format(player, ex))

        elif self.player.mod:
            if action == "join_channel":
                user_ids = message['user_ids']
                channel = message['channel']

                for user_id in user_ids:
                    player = self.player_service[message[user_id]]
                    if player:
                        player.lobby_connection.sendJSON(dict(command="social", autojoin=[channel]))

    async def check_user_login(self, conn, login, password):
        # TODO: Hash passwords server-side so the hashing actually *does* something.
        result = await conn.execute(
            "SELECT login.id as id,"
            "login.login as username,"
            "login.password as password,"
            "login.steamid as steamid,"
            "login.create_time as create_time,"
            "lobby_ban.reason as reason,"
            "lobby_ban.expires_at as expires_at "
            "FROM login "
            "LEFT JOIN lobby_ban ON login.id = lobby_ban.idUser "
            "WHERE LOWER(login)=%s "
            "ORDER BY expires_at DESC", (login.lower(), ))

        auth_error_message = "Login not found or password incorrect. They are case sensitive."
        row = await result.fetchone()
        if not row:
            raise AuthenticationError(auth_error_message)

        player_id, real_username, dbPassword, steamid, create_time, ban_reason, ban_expiry = (row[i] for i in range(7))

        if dbPassword != password:
            raise AuthenticationError(auth_error_message)

        now = datetime.datetime.now()

        if ban_reason is not None and now < ban_expiry:
            self._logger.debug('Rejected login from banned user: %s, %s, %s', player_id, login, self.session)
            raise ClientError("You are banned from FAF.\n Reason :\n {}".format(ban_reason), recoverable=False)

        # New accounts are prevented from playing if they didn't link to steam

        if config.FORCE_STEAM_LINK and not steamid and create_time.timestamp() > config.FORCE_STEAM_LINK_AFTER_DATE:
            self._logger.debug('Rejected login from new user: %s, %s, %s', player_id, login, self.session)
            raise ClientError(
                "Unfortunately, you must currently link your account to Steam in order to play Forged Alliance Forever. You can do so on <a href='{steamlink_url}'>{steamlink_url}</a>.".format(steamlink_url=config.WWW_URL + '/account/link'),
                recoverable=False)

        self._logger.debug("Login from: %s, %s, %s", player_id, login, self.session)

        return player_id, real_username, steamid

    def check_version(self, message):
        versionDB, updateFile = self.player_service.client_version_info
        update_msg = dict(command="update",
                          update=updateFile,
                          new_version=versionDB)

        self.user_agent = message.get('user_agent')
        version = message.get('version')
        server.stats.gauge('user.agents.None', -1, delta=True)
        server.stats.gauge('user.agents.{}'.format(self.user_agent), 1, delta=True)

        if not version or not self.user_agent:
            update_msg['command'] = 'welcome'
            # For compatibility with 0.10.x updating mechanism
            self.sendJSON(update_msg)
            return False

        # Check their client is reporting the right version number.
        if 'downlords-faf-client' not in self.user_agent:
            try:
                if "-" in version:
                    version = version.split('-')[0]
                if "+" in version:
                    version = version.split('+')[0]
                if semver.compare(versionDB, version) > 0:
                    self.sendJSON(update_msg)
                    return False
            except ValueError:
                self.sendJSON(update_msg)
                return False
        return True

    async def check_policy_conformity(self, player_id, uid_hash, session):
        url = FAF_POLICY_SERVER_BASE_URL + '/verify'
        payload = dict(player_id=player_id, uid_hash=uid_hash, session=session)
        headers = {
            'content-type': "application/json",
            'cache-control': "no-cache"
        }

        response = requests.post(url, json=payload, headers=headers).json()

        if response.get('result', '') == 'vm':
            self._logger.debug("Using VM: %d: %s", player_id, uid_hash)
            self.sendJSON(dict(command="notice", style="error",
                               text="You need to link your account to Steam in order to use FAF in a virtual machine. "
                                    "Please contact an admin or moderator on the forums if you feel this is a false positive."))
            self.send_warning("Your computer seems to be a virtual machine.<br><br>In order to "
                              "log in from a VM, you have to link your account to Steam: <a href='" +
                              config.WWW_URL + "/account/link'>" +
                              config.WWW_URL + "/account/link</a>.<br>If you need an exception, please contact an "
                                               "admin or moderator on the forums", fatal=True)

        if response.get('result', '') == 'already_associated':
            self._logger.warning("UID hit: %d: %s", player_id, uid_hash)
            self.send_warning("Your computer is already associated with another FAF account.<br><br>In order to "
                              "log in with an additional account, you have to link it to Steam: <a href='" +
                              config.WWW_URL + "/account/link'>" +
                              config.WWW_URL + "/account/link</a>.<br>If you need an exception, please contact an "
                                               "admin or moderator on the forums", fatal=True)
            return False

        if response.get('result', '') == 'fraudulent':
            self._logger.info("Banning player %s for fraudulent looking login.", player_id)
            self.send_warning("Fraudulent login attempt detected. As a precautionary measure, your account has been "
                              "banned permanently. Please contact an admin or moderator on the forums if you feel this is "
                              "a false positive.",
                              fatal=True)

            with await db.engine.acquire() as conn:
                try:
                    await conn.execute(
                        "INSERT INTO ban (player_id, author_id, reason, level) VALUES (%s, %s, %s, 'GLOBAL')",
                        (player_id, player_id, "Auto-banned because of fraudulent login attempt"))
                except pymysql.MySQLError as e:
                    raise ClientError('Banning failed: {}'.format(e))

            return False

        return response.get('result', '') == 'honest'

    async def command_hello(self, message):
        login = message['login'].strip()
        password = message['password']

        async with db.engine.acquire() as conn:
            player_id, login, steamid = await self.check_user_login(conn, login, password)
            server.stats.incr('user.logins')
            server.stats.gauge('users.online', len(self.player_service))

            await conn.execute(
                "UPDATE login SET ip = %(ip)s, user_agent = %(user_agent)s, last_login = NOW() WHERE id = %(player_id)s",
                {
                    "ip": self.peer_address.host,
                    "user_agent": self.user_agent,
                    "player_id": player_id
                })

            if not self.player_service.is_uniqueid_exempt(player_id) and steamid is None:
                conforms_policy = await self.check_policy_conformity(player_id, message['unique_id'], self.session)
                if not conforms_policy:
                    return

            # Update the user's IRC registration (why the fuck is this here?!)
            m = hashlib.md5()
            m.update(password.encode())
            passwordmd5 = m.hexdigest()
            m = hashlib.md5()
            # Since the password is hashed on the client, what we get at this point is really
            # md5(md5(sha256(password))). This is entirely insane.
            m.update(passwordmd5.encode())
            irc_pass = "md5:" + str(m.hexdigest())

            try:
                await conn.execute("UPDATE anope.anope_db_NickCore SET pass = %s WHERE display = %s", (irc_pass, login))
            except (pymysql.OperationalError, pymysql.ProgrammingError):
                self._logger.error("Failure updating NickServ password for %s", login)

        permission_group = self.player_service.get_permission_group(player_id)
        self.player = Player(login=str(login),
                             session=self.session,
                             ip=self.peer_address.host,
                             port=None,
                             id=player_id,
                             permissionGroup=permission_group,
                             lobby_connection=self)
        self.connectivity = Connectivity(self, self.peer_address.host, self.player)

        if self.player.id in self.player_service and self.player_service[self.player.id].lobby_connection:
            old_conn = self.player_service[self.player.id].lobby_connection
            old_conn.send_warning("You have been signed out because you signed in elsewhere.", fatal=True)

        await self.player_service.fetch_player_data(self.player)

        self.player_service[self.player.id] = self.player
        self._authenticated = True

        # Country
        # -------
        self.player.country = self.geoip_service.country(self.peer_address.host)

        ## AVATARS
        ## -------------------
        async with db.engine.acquire() as conn:
            result = await conn.execute(
                "SELECT url, tooltip FROM `avatars` "
                "LEFT JOIN `avatars_list` ON `idAvatar` = `avatars_list`.`id` "
                "WHERE `idUser` = %s AND `selected` = 1", (self.player.id, ))
            row = await result.fetchone()
            if row:
                self.player.avatar = {"url": row["url"], "tooltip": row['tooltip']}

        # Send the player their own player info.
        self.sendJSON({
            "command": "welcome",
            "me": self.player.to_dict(),

            # For backwards compatibility for old clients. For now.
            "id": self.player.id,
            "login": login
        })

        # Tell player about everybody online. This must happen after "welcome".
        self.sendJSON(
            {
                "command": "player_info",
                "players": [player.to_dict() for player in self.player_service]
            }
        )

        # Tell everyone else online about us. This must happen after all the player_info messages.
        # This ensures that no other client will perform an operation that interacts with the
        # incoming user, allowing the client to make useful assumptions: it can be certain it has
        # initialised its local player service before it is going to get messages that want to
        # query it.
        self.player_service.mark_dirty(self.player)

        friends = []
        foes = []
        async with db.engine.acquire() as conn:
            result = await conn.execute(
                "SELECT `subject_id`, `status` "
                "FROM friends_and_foes WHERE user_id = %s", (self.player.id,))

            async for row in result:
                target_id, status = row["subject_id"], row["status"]
                if status == "FRIEND":
                    friends.append(target_id)
                else:
                    foes.append(target_id)

        self.player.friends = set(friends)
        self.player.foes = set(foes)

        channels = []
        if self.player.mod:
            channels.append("#moderators")

        if self.player.clan is not None:
            channels.append("#%s_clan" % self.player.clan)

        json_to_send = {"command": "social", "autojoin": channels, "channels": channels, "friends": friends, "foes": foes, "power": permission_group}
        self.sendJSON(json_to_send)

        self.send_mod_list()
        self.send_game_list()
        await self.send_tutorial_section()

    @timed
    def command_ask_session(self, message):
        if self.check_version(message):
            self.sendJSON({
                "command": "session",
                "session": self.session
            })

    async def command_avatar(self, message):
        action = message['action']

        if action == "list_avatar":
            avatarList = []

            async with db.engine.acquire() as conn:
                result = await conn.execute(
                    "SELECT url, tooltip FROM `avatars` "
                    "LEFT JOIN `avatars_list` ON `idAvatar` = `avatars_list`.`id` WHERE `idUser` = %s", (self.player.id,))

                async for row in result:
                    avatar = {"url": row["url"], "tooltip": row["tooltip"]}
                    avatarList.append(avatar)

                if len(avatarList) > 0:
                    self.sendJSON({"command": "avatar", "avatarlist": avatarList})

        elif action == "select":
            avatar = message['avatar']

            async with db.engine.acquire() as conn:
                await conn.execute(
                    "UPDATE `avatars` SET `selected` = 0 WHERE `idUser` = %s", (self.player.id, ))
                if avatar is not None:
                    await conn.execute(
                        "UPDATE `avatars` SET `selected` = 1 WHERE `idAvatar` ="
                        "(SELECT id FROM avatars_list WHERE avatars_list.url = %s) and "
                        "`idUser` = %s", (avatar, self.player.id))
        else:
            raise KeyError('invalid action')

    @property
    def able_to_launch_game(self):
        return self.connectivity.result

    @timed
    def command_game_join(self, message):
        """
        We are going to join a game.
        """
        assert isinstance(self.player, Player)
        if not self.able_to_launch_game:
            raise ClientError("You are already in a game or haven't run the connectivity test yet")

        if self.connectivity.result.state == ConnectivityState.STUN:
            self.connectivity.relay_address = Address(*message['relay_address'])

        uuid = int(message['uid'])
        port = int(message['gameport'])
        password = message.get('password', None)

        self._logger.debug("joining: %d:%d with pw: %s", uuid, port, password)
        try:
            game = self.game_service[uuid]
            if not game or game.state != GameState.LOBBY:
                self._logger.debug("Game not in lobby state: %s", game)
                self.sendJSON(dict(command="notice", style="info", text="The game you are trying to join is not ready."))
                return

            if game.password != password:
                self.sendJSON(dict(command="notice", style="info", text="Bad password (it's case sensitive)"))
                return

            self.launch_game(game, port, is_host=False)
        except KeyError:
            self.sendJSON(dict(command="notice", style="info", text="The host has left the game"))

    async def command_game_matchmaking(self, message):
        mod = message.get('mod', 'ladder1v1')
        port = message.get('gameport', None)
        state = message['state']

        if not self.able_to_launch_game:
            raise ClientError("You are already in a game or are otherwise having connection problems. Please report this issue using HELP -> Tech support.")

        if state == "stop":
            if self.search:
                self._logger.info("%s stopped searching for ladder: %s", self.player, self.search)
                self.search.cancel()
            return

        if self.connectivity.result.state == ConnectivityState.STUN:
            self.connectivity.relay_address = Address(*message['relay_address'])

        if port:
            self.player.game_port = port

        async with db.engine.acquire() as conn:
            result = await conn.execute("SELECT id FROM matchmaker_ban WHERE `userid` = %s", (self.player.id))
            row = await result.fetchone()
            if row:
                self.sendJSON(dict(command="notice", style="error",
                                   text="You are banned from the matchmaker. Contact an admin to have the reason."))
                return

        if mod == "ladder1v1":
            if state == "start":
                if self.search:
                    self.search.cancel()
                assert self.player is not None
                self.player.faction = message['faction']
                self.search = Search(self.player)

                self.game_service.ladder_service.inform_player(self.player)

                self._logger.info("%s is searching for ladder: %s", self.player, self.search)
                asyncio.ensure_future(self.matchmaker_queue.search(self.search))

    def command_coop_list(self, message):
        """ Request for coop map list"""
        asyncio.ensure_future(self.send_coop_maps())

    @timed()
    def command_game_host(self, message):
        if not self.able_to_launch_game:
            raise ClientError("You are already in a game or haven't run the connectivity test yet")

        if self.connectivity.result.state == ConnectivityState.STUN:
            self.connectivity.relay_address = Address(*message['relay_address'])

        assert isinstance(self.player, Player)

        visibility = VisibilityState.from_string(message.get('visibility'))
        if not isinstance(visibility, VisibilityState):
            # Protocol violation.
            self.abort("{} sent a nonsense visibility code: {}".format(self.player.login, message.get('visibility')))
            return

        title = html.escape(message.get('title') or f"{self.player.login}'s game")

        try:
            title.encode('ascii')
        except UnicodeEncodeError:
            self.sendJSON(dict(command="notice", style="error", text="Non-ascii characters in game name detected."))
            return

        port = message.get('gameport')
        mod = message.get('mod')
        mapname = message.get('mapname')
        password = message.get('password')
        game_mode = GameMode.from_string(mod.lower())

        game = self.game_service.create_game(
            visibility=visibility,
            game_mode=game_mode,
            host=self.player,
            name=title,
            mapname=mapname or 'scmp_007',
            password=password
        )
        self.launch_game(game, port, is_host=True)
        server.stats.incr('game.hosted')

    def launch_game(self, game, port, is_host=False, use_map=None):
        # TODO: Fix setting up a ridiculous amount of cyclic pointers here
        if self.game_connection:
            self.game_connection.abort("Player launched a new game")

        if is_host:
            game.host = self.player

        self.game_connection = GameConnection(
            game=game,
            player=self.player,
            protocol=self.protocol,
            connectivity=self.connectivity,
            player_service=self.player_service,
            games=self.game_service
        )

        self.player.state = PlayerState.HOSTING if is_host else PlayerState.JOINING
        self.player.game = game
        self.player.game_port = port
        cmd = {
            "command": "game_launch",
            "mod": game.game_mode,
            "uid": game.id,
            "args": ["/numgames " + str(self.player.numGames)]
        }
        if use_map:
            cmd['mapname'] = use_map
        self.sendJSON(cmd)

    async def command_modvault(self, message):
        type = message["type"]

        async with db.engine.acquire() as conn:
            if type == "start":
                result = await conn.execute("SELECT uid, name, version, author, ui, date, downloads, likes, played, description, filename, icon FROM table_mod ORDER BY likes DESC LIMIT 100")

                async for row in result:
                    uid, name, version, author, ui, date, downloads, likes, played, description, filename, icon = (row[i] for i in range(12))
                    try:
                        link = urllib.parse.urljoin(config.CONTENT_URL, "faf/vault/" + filename)
                        thumbstr = ""
                        if icon != "":
                            thumbstr = urllib.parse.urljoin(config.CONTENT_URL, "faf/vault/mods_thumbs/" + urllib.parse.quote(icon))

                        out = dict(command="modvault_info", thumbnail=thumbstr, link=link, bugreports=[],
                                   comments=[], description=description, played=played, likes=likes,
                                   downloads=downloads, date=int(date.timestamp()), uid=uid, name=name, version=version, author=author,
                                   ui=ui)
                        self.sendJSON(out)
                    except:
                        self._logger.error("Error handling table_mod row (uid: {})".format(uid), exc_info=True)
                        pass

            elif type == "like":
                canLike = True
                uid = message['uid']
                result = await conn.execute("SELECT uid, name, version, author, ui, date, downloads, likes, played, description, filename, icon, likers FROM `table_mod` WHERE uid = %s LIMIT 1", (uid,))

                row = await result.fetchone()
                uid, name, version, author, ui, date, downloads, likes, played, description, filename, icon, likerList = (row[i] for i in range(13))
                link = urllib.parse.urljoin(config.CONTENT_URL, "faf/vault/" + filename)
                thumbstr = ""
                if icon != "":
                    thumbstr = urllib.parse.urljoin(config.CONTENT_URL, "faf/vault/mods_thumbs/" + urllib.parse.quote(icon))

                out = dict(command="modvault_info", thumbnail=thumbstr, link=link, bugreports=[],
                           comments=[], description=description, played=played, likes=likes + 1,
                           downloads=downloads, date=int(date.timestamp()), uid=uid, name=name, version=version, author=author,
                           ui=ui)

                try:
                    likers = json.loads(likerList)
                    if self.player.id in likers:
                        canLike = False
                    else:
                        likers.append(self.player.id)
                except:
                    likers = []

                # TODO: Avoid sending all the mod info in the world just because we liked it?
                if canLike:
                    await conn.execute(
                        "UPDATE mod_stats s "
                        "JOIN mod_version v ON v.mod_id = s.mod_id "
                        "SET s.likes = s.likes + 1, likers=%s WHERE v.uid = %s",
                        json.dumps(likers), uid)
                    self.sendJSON(out)

            elif type == "download":
                uid = message["uid"]
                await conn.execute(
                    "UPDATE mod_stats s "
                    "JOIN mod_version v ON v.mod_id = s.mod_id "
                    "SET downloads=downloads+1 WHERE v.uid = %s", uid)
            else:
                raise ValueError('invalid type argument')

    def send_warning(self, message: str, fatal: bool=False):
        """
        Display a warning message to the client
        :param message: Warning message to display
        :param fatal: Whether or not the warning is fatal.
                      If the client receives a fatal warning it should disconnect
                      and not attempt to reconnect.
        :return: None
        """
        self.sendJSON({'command': 'notice',
                       'style': 'info' if not fatal else 'error',
                       'text': message})
        if fatal:
            self.abort(message)

    def send(self, message):
        """

        :param message:
        :return:
        """
        self._logger.debug(">>: %s", message)
        self.protocol.send_message(message)

    async def drain(self):
        await self.protocol.drain()

    def sendJSON(self, data_dictionary):
        """
        Deprecated alias for send
        """
        self.send(data_dictionary)

    async def on_connection_lost(self):
        async def nopdrain(message):
            return
        self.drain = nopdrain
        self.send = lambda m: None
        if self.game_connection:
            await self.game_connection.on_connection_lost()
        if self.search and not self.search.done():
            self.search.cancel()
        if self.player:
            self.player_service.remove_player(self.player)
