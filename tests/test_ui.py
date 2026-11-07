import re
from urllib.parse import parse_qs, urlsplit

from app.models import Ticket, TicketStatus
from app.web.ui import PAGE_SIZE

TICKET_ID_RE = re.compile(r'<div class="ticket" id="t(\d+)">')


async def _seed(sessionmaker, new: int, rejected: int) -> None:
    async with sessionmaker.begin() as session:
        session.add_all(
            Ticket(
                event_id=f"evt_{n}",
                external_ticket_id=f"T-{n}",
                customer_email="u@example.com",
                message=f"Звернення {n}",
                reply_url="https://helpdesk.test/r",
                # rejected ones are the oldest, so they land on the last page of "all"
                status=TicketStatus.REJECTED
                if n < rejected
                else TicketStatus.NEW,
                draft_text="чернетка" if n < rejected else None,
            )
            for n in range(new + rejected)
        )


def _ids(html: str) -> list[int]:
    return [int(x) for x in TICKET_ID_RE.findall(html)]


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


async def test_pages_split_the_filtered_list_without_gaps_or_overlaps(
    client, sessionmaker
):
    await _seed(sessionmaker, new=2 * PAGE_SIZE + 20, rejected=7)

    pages = [
        await client.get("/ui/tickets", params={"status": "new", "page": n})
        for n in (1, 2, 3)
    ]
    ids = [_ids(p.text) for p in pages]
    assert [len(x) for x in ids] == [PAGE_SIZE, PAGE_SIZE, 20]
    flat = [i for page in ids for i in page]
    assert len(set(flat)) == len(flat) == 2 * PAGE_SIZE + 20
    # Newest first, continuous across pages.
    assert flat == sorted(flat, reverse=True)
    assert "Сторінка 2 з 3 (120 звернень)" in pages[1].text


async def test_pager_links_keep_the_status_filter(client, sessionmaker):
    await _seed(sessionmaker, new=2 * PAGE_SIZE + 20, rejected=7)

    page = await client.get("/ui/tickets", params={"status": "new", "page": 2})
    prev_url = (
        re.search(r'href="([^"]+)" rel="prev"', page.text)
        .group(1)
        .replace("&amp;", "&")
    )
    next_url = (
        re.search(r'href="([^"]+)" rel="next"', page.text)
        .group(1)
        .replace("&amp;", "&")
    )
    assert _query(prev_url) == {
        "status": ["new"]
    }  # page 1 needs no page parameter
    assert _query(next_url) == {"status": ["new"], "page": ["3"]}

    last = await client.get(next_url)
    assert 'rel="next"' not in last.text
    assert 'rel="prev"' in last.text


async def test_action_forms_carry_the_current_filter_and_page(
    client, sessionmaker
):
    await _seed(sessionmaker, new=2 * PAGE_SIZE + 20, rejected=7)
    last_of_all = await client.get("/ui/tickets", params={"page": 3})
    # Rejected tickets have action forms.
    assert "/regenerate" in last_of_all.text
    assert '<input type="hidden" name="page" value="3">' in last_of_all.text
    assert '<input type="hidden" name="status" value="">' in last_of_all.text


async def test_single_page_has_no_pager_and_bad_page_numbers_are_clamped(
    client, sessionmaker
):
    await _seed(sessionmaker, new=2 * PAGE_SIZE + 20, rejected=7)

    rejected = await client.get("/ui/tickets", params={"status": "rejected"})
    assert len(_ids(rejected.text)) == 7
    assert "Сторінка" not in rejected.text
    # StrEnum members render as their value, without `.value`.
    assert '<input type="hidden" name="status" value="rejected">' in (
        rejected.text
    )
    assert 'href="/ui/tickets?status=new"' in rejected.text
    assert "Відхилено (7)" in rejected.text

    too_far = await client.get(
        "/ui/tickets", params={"status": "new", "page": 99}
    )
    assert "Сторінка 3 з 3" in too_far.text
    garbage = await client.get(
        "/ui/tickets", params={"status": "new", "page": "abc"}
    )
    assert garbage.status_code == 200
    assert "Сторінка 1 з 3" in garbage.text


async def test_action_redirects_back_to_the_same_filter_and_page(
    client, sessionmaker
):
    await _seed(sessionmaker, new=2 * PAGE_SIZE + 20, rejected=7)
    page = await client.get("/ui/tickets", params={"status": "rejected"})
    ticket_id = _ids(page.text)[0]

    response = await client.post(
        f"/ui/tickets/{ticket_id}/regenerate",
        data={"status": "new", "page": "2"},
    )
    assert response.status_code == 303
    query = _query(response.headers["location"])
    assert query["status"] == ["new"]
    assert query["page"] == ["2"]
    assert "notice" in query


async def test_api_supports_offset(client, sessionmaker):
    await _seed(sessionmaker, new=30, rejected=0)
    first = (await client.get("/api/tickets", params={"limit": 20})).json()
    rest = (
        await client.get("/api/tickets", params={"limit": 20, "offset": 20})
    ).json()
    assert len(first) == 20
    assert len(rest) == 10
    assert not {t["id"] for t in first} & {t["id"] for t in rest}
