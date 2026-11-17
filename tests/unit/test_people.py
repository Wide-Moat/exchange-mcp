"""people: what list_people sends to EWS, what it returns, and every refusal in docs/requirements.md."""

from __future__ import annotations

from conftest import envelope, error, ok


def resolution(name: str, address: str, kind: str = "Mailbox", item: str = "") -> str:
    iid = f'<t:ItemId Id="{item}" ChangeKey="CK"/>' if item else ""
    return (
        f"<t:Resolution><t:Mailbox><t:Name>{name}</t:Name><t:EmailAddress>{address}</t:EmailAddress>"
        f"<t:RoutingType>SMTP</t:RoutingType><t:MailboxType>{kind}</t:MailboxType>{iid}</t:Mailbox>"
        f"<t:Contact><t:DisplayName>{name}</t:DisplayName><t:JobTitle>Engineer</t:JobTitle>"
        '<t:PhoneNumbers><t:Entry Key="BusinessPhone">+1 555 0100</t:Entry></t:PhoneNumbers></t:Contact>'
        "</t:Resolution>"
    )


def resolved(*resolutions: str) -> bytes:
    return ok(
        "ResolveNames",
        f'<m:ResolutionSet TotalItemsInView="{len(resolutions)}" IncludesLastItemInRange="true">'
        f"{''.join(resolutions)}</m:ResolutionSet>",
    )


async def test_an_empty_query_is_refused(rt, ews, invoke):
    for source in ("all", "directory", "contacts"):
        for q in (None, "", "   "):
            args = {"source": source} if q is None else {"source": source, "query": q}
            r = await invoke("list_people", **args)
            assert r.is_error and "Give a query" in r.text, source
    assert ews.ops() == []


async def test_distribution_lists_are_not_expanded(rt, ews, invoke):
    ews.on("ResolveNames", resolved(resolution("All Staff", "all@corp.example", "PublicDL")))
    r = await invoke("list_people", query="All Staff")
    assert not r.is_error, r.text
    row = r.data["items"][0]
    assert row["kind"] == "distribution list" and "members" not in row
    assert ews.ops() == ["ResolveNames"]
    assert ews.writes() == []


async def test_each_result_says_where_it_came_from(rt, ews, invoke):
    ews.on(
        "ResolveNames",
        resolved(
            resolution("Pat Doe", "pat@corp.example"),
            resolution("Pat Friend", "pat@home.example", "Contact", item="C1"),
        ),
    )
    r = await invoke("list_people", query="Pat")
    assert not r.is_error, r.text
    assert [(x["source"], x.get("id")) for x in r.data["items"]] == [("directory", None), ("contacts", "C1")]
    assert r.data["items"][0]["title"] == "Engineer" and r.data["items"][0]["phone"] == "+1 555 0100"
    sent = ews.last("ResolveNames").body
    assert sent.get("SearchScope") == "ActiveDirectoryContacts" and sent.get("ReturnFullContactData") == "true"
    assert "ChangeKey" not in str(r.data)


async def test_directory_only_searches_the_address_book(rt, ews, invoke):
    ews.on("ResolveNames", error("ResolveNames", "ErrorNameResolutionNoResults"))
    r = await invoke("list_people", query="Nobody", source="directory")
    assert not r.is_error and r.data["items"] == []
    assert ews.last("ResolveNames").body.get("SearchScope") == "ActiveDirectory"


async def test_contacts_come_from_the_contacts_folder_with_ids(rt, ews, invoke):
    ews.on(
        "FindItem",
        ok(
            "FindItem",
            '<m:RootFolder TotalItemsInView="3" IncludesLastItemInRange="false"><t:Items>'
            '<t:Contact><t:ItemId Id="C1" ChangeKey="K"/><t:DisplayName>Pat Friend</t:DisplayName>'
            '<t:EmailAddresses><t:Entry Key="EmailAddress1">pat@home.example</t:Entry></t:EmailAddresses>'
            "</t:Contact></t:Items></m:RootFolder>",
        ),
    )
    r = await invoke("list_people", query="pat", source="contacts", limit=1)
    assert not r.is_error, r.text
    assert r.data["items"] == [{"name": "Pat Friend", "address": "pat@home.example", "source": "contacts", "id": "C1"}]
    assert r.data["has_more"]
    sent = ews.last("FindItem").xml()
    assert 'Id="contacts"' in sent and 'Value="pat"' in sent


async def test_rooms_list_without_a_query(rt, ews, invoke):
    ews.on(
        "GetRoomLists",
        envelope(
            '<m:GetRoomListsResponse ResponseClass="Success"><m:ResponseCode>NoError</m:ResponseCode><m:RoomLists>'
            "<t:Address><t:Name>HQ</t:Name><t:EmailAddress>hq-rooms@corp.example</t:EmailAddress></t:Address>"
            "</m:RoomLists></m:GetRoomListsResponse>"
        ),
    )
    ews.on(
        "GetRooms",
        envelope(
            '<m:GetRoomsResponse ResponseClass="Success"><m:ResponseCode>NoError</m:ResponseCode><m:Rooms>'
            "<t:Room><t:Id><t:Name>Board</t:Name><t:EmailAddress>board@corp.example</t:EmailAddress></t:Id></t:Room>"
            "<t:Room><t:Id><t:Name>Focus</t:Name><t:EmailAddress>focus@corp.example</t:EmailAddress></t:Id></t:Room>"
            "</m:Rooms></m:GetRoomsResponse>"
        ),
    )
    r = await invoke("list_people", source="rooms")
    assert not r.is_error, r.text
    assert [x["name"] for x in r.data["items"]] == ["Board", "Focus"]
    assert all(x["source"] == "rooms" and x["room_list"] == "HQ" for x in r.data["items"])
    r = await invoke("list_people", source="rooms", query="foc")
    assert [x["name"] for x in r.data["items"]] == ["Focus"]


async def test_a_failed_room_list_is_an_error(rt, ews, invoke):
    ews.on(
        "GetRoomLists",
        envelope(
            '<m:GetRoomListsResponse ResponseClass="Error"><m:ResponseCode>ErrorAccessDenied</m:ResponseCode>'
            "</m:GetRoomListsResponse>"
        ),
    )
    r = await invoke("list_people", source="rooms")
    assert r.is_error and "does not let you" in r.text
