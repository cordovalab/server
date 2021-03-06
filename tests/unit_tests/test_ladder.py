from unittest import mock

import pytest
from server import GameService, LadderService
from server.db.models import game_player_stats, game_stats
from server.players import Player
from sqlalchemy import func, text
from tests import CoroMock


@pytest.fixture
def ladder_service(game_service: GameService):
    return LadderService(game_service)


async def test_start_game(ladder_service: LadderService, game_service: GameService):
    p1 = mock.create_autospec(Player('Dostya', id=1))
    p2 = mock.create_autospec(Player('Rhiza', id=2))

    p1.id = 1
    p2.id = 2
    game_service.ladder_maps = [(1, 'scmp_007', 'maps/scmp_007.zip')]

    with mock.patch('asyncio.sleep', CoroMock()):
        await ladder_service.start_game(p1, p2)

    assert p1.lobby_connection.launch_game.called
    assert p2.lobby_connection.launch_game.called


def test_inform_player(ladder_service: LadderService):
    p1 = mock.create_autospec(Player('Dostya', id=1))
    p1.ladder_rating = (1500, 500)

    ladder_service.inform_player(p1)

    assert p1.lobby_connection.sendJSON.called


async def test_choose_map(ladder_service: LadderService):
    ladder_service.get_ladder_history = CoroMock(
        return_value=[1, 2, 3]
    )

    ladder_service.game_service.ladder_maps = [
        (1, "some_map", "maps/some_map.v001.zip"),
        (2, "some_map", "maps/some_map.v001.zip"),
        (3, "some_map", "maps/some_map.v001.zip"),
        (4, "CHOOSE_ME", "maps/choose_me.v001.zip"),
    ]

    chosen_map = await ladder_service.choose_map([None])

    # Make the probability very low that the test passes because we got lucky
    for _ in range(20):
        assert chosen_map == (4, "CHOOSE_ME", "maps/choose_me.v001.zip")


async def test_choose_map_all_maps_played(ladder_service: LadderService):
    ladder_service.get_ladder_history = CoroMock(
        return_value=[1, 2, 3]
    )

    ladder_service.game_service.ladder_maps = [
        (1, "some_map", "maps/some_map.v001.zip"),
        (2, "some_map", "maps/some_map.v001.zip"),
        (3, "some_map", "maps/some_map.v001.zip"),
    ]

    chosen_map = await ladder_service.choose_map([None])

    assert chosen_map is not None


async def test_choose_map_raises_on_empty_map_pool(ladder_service: LadderService):
    ladder_service.game_service.ladder_maps = []

    with pytest.raises(RuntimeError):
        await ladder_service.choose_map([])


async def test_get_ladder_history(ladder_service: LadderService, players, db_engine):
    history = await ladder_service.get_ladder_history(players.hosting, limit=1)
    assert history == [6]


async def test_get_ladder_history_many_maps(ladder_service: LadderService, players, db_engine):
    history = await ladder_service.get_ladder_history(players.hosting, limit=4)
    assert history == [6, 5, 4, 3]
