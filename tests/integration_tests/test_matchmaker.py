import pytest

from .conftest import connect_and_sign_in, read_until
from .testclient import ClientTest


async def test_game_matchmaking(loop, lobby_server):
    _, _, proto1 = await connect_and_sign_in(
        ('ladder1', 'ladder1'),
        lobby_server
    )
    _, _, proto2 = await connect_and_sign_in(
        ('ladder2', 'ladder2'),
        lobby_server
    )

    await read_until(proto1, lambda msg: msg['command'] == 'game_info')
    await read_until(proto2, lambda msg: msg['command'] == 'game_info')

    with ClientTest(loop=loop, process_nat_packets=True, proto=proto1) as client1:
        with ClientTest(loop=loop, process_nat_packets=True, proto=proto2) as client2:
            port1, port2 = 6112, 6113
            await client1.listen_udp(port=port1)
            await client1.perform_connectivity_test(port=port1)

            await client2.listen_udp(port=port2)
            await client2.perform_connectivity_test(port=port2)

            proto1.send_message({
                'command': 'game_matchmaking',
                'state': 'start',
                'faction': 'uef'
            })
            await proto1.drain()

            proto2.send_message({
                'command': 'game_matchmaking',
                'state': 'start',
                'faction': 'uef'
            })
            await proto2.drain()

            # If the players did not match, this test will fail due to a timeout error
            msg1 = await read_until(proto1, lambda msg: msg['command'] == 'game_launch')
            msg2 = await read_until(proto2, lambda msg: msg['command'] == 'game_launch')

            assert msg1['uid'] == msg2['uid']
            assert msg1['mod'] == 'ladder1v1'
            assert msg2['mod'] == 'ladder1v1'


async def test_game_matchmaking_ban(loop, lobby_server, db_engine):
    _, _, proto = await connect_and_sign_in(
        ('ladder_ban', 'ladder_ban'),
        lobby_server
    )

    await read_until(proto, lambda msg: msg['command'] == 'game_info')

    with ClientTest(loop=loop, process_nat_packets=True, proto=proto) as client1:
        port = 6112
        await client1.listen_udp(port=port)
        await client1.perform_connectivity_test(port=port)

        proto.send_message({
            'command': 'game_matchmaking',
            'state': 'start',
            'faction': 'uef'
        })
        await proto.drain()

        # This may fail due to a timeout error
        msg = await read_until(proto, lambda msg: msg['command'] == 'notice')

        assert msg == {
            'command': 'notice',
            'style': 'error',
            'text': 'You are banned from the matchmaker. Contact an admin to have the reason.'
        }
