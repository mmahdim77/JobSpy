from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from jobspy.model import LinkedInJobDetail


DEFAULT_PROFILE_DIR = Path(".linkedin-playwright-profile")


def normalize_linkedin_job(job_id_or_url: str) -> tuple[str, str]:
    if job_id_or_url.startswith("http://") or job_id_or_url.startswith("https://"):
        match = re.search(r"/jobs/view/(\d+)", job_id_or_url)
        job_id = match.group(1) if match else job_id_or_url.rstrip("/").split("/")[-1]
        return job_id.removeprefix("li-").strip(), job_id_or_url

    job_id = job_id_or_url.removeprefix("li-").strip()
    return job_id, f"https://www.linkedin.com/jobs/view/{job_id}"


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    value = re.sub(r"\s+", " ", value).strip()
    return value or None


def _locator_text(locator) -> str | None:
    try:
        return _clean_text(locator.inner_text())
    except Exception:
        return None


def _locator_attr(locator, attr: str) -> str | None:
    try:
        value = locator.get_attribute(attr)
    except Exception:
        return None
    return _clean_text(value)


def _text_or_none(page, selectors: list[str]) -> str | None:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.count() and locator.is_visible():
                text = _clean_text(locator.inner_text())
                if text:
                    return text
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue
    return None


def _texts(page, selectors: list[str]) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for selector in selectors:
        locator = page.locator(selector)
        try:
            count = locator.count()
        except Exception:
            continue
        for index in range(count):
            try:
                text = _clean_text(locator.nth(index).inner_text())
            except Exception:
                continue
            if text and text not in seen:
                seen.add(text)
                values.append(text)
    return values


def _non_empty_lines(text: str | None) -> list[str]:
    if not text:
        return []
    return [line.strip() for line in text.splitlines() if line.strip()]


def _header_lines(page) -> list[str]:
    main_text = _locator_text(page.locator("main").first)
    lines = _non_empty_lines(main_text)
    if "About the job" in lines:
        return lines[: lines.index("About the job")]
    return lines[:25]


def _top_action_candidates(page) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    locator = page.locator("main a, main button")
    try:
        count = locator.count()
    except Exception:
        return candidates

    for index in range(count):
        element = locator.nth(index)
        try:
            text = _locator_text(element)
            box = element.bounding_box()
            href = _locator_attr(element, "href")
        except Exception:
            continue
        if not text or not box:
            continue
        if box.get("y", 10_000) > 900:
            continue
        candidates.append(
            {"text": text, "href": href, "y": box.get("y"), "x": box.get("x")}
        )

    candidates.sort(key=lambda item: (item["y"], item["x"]))
    return candidates


def _detect_login_wall(page) -> bool:
    login_indicators = [
        "form.login__form",
        "input#username",
        "a[href*='/login']",
        "a[href*='/signup']",
        ".authwall",
    ]
    if any(token in page.url for token in ("/login", "/signup")):
        return True
    for selector in login_indicators:
        try:
            if page.locator(selector).count():
                return True
        except Exception:
            continue
    return False


def _wait_for_job_page(page) -> None:
    page.wait_for_load_state("domcontentloaded", timeout=45_000)

    stable_selectors = [
        "h1.t-24",
        ".job-details-jobs-unified-top-card__job-title",
        ".jobs-unified-top-card",
        ".jobs-search__job-details--container",
        ".jobs-description",
        ".scaffold-layout__detail",
        "main",
    ]
    for selector in stable_selectors:
        try:
            page.locator(selector).first.wait_for(state="visible", timeout=8_000)
            return
        except Exception:
            continue
    page.wait_for_timeout(3_000)


def _maybe_expand_description(page) -> None:
    buttons = [
        "button[aria-label*='Click to see more description']",
        "button[aria-label*='see more description']",
        "button.jobs-description__footer-button",
        "button:has-text('Show more')",
        "button:has-text('See more')",
        "a:has-text('… more')",
    ]
    for selector in buttons:
        locator = page.locator(selector).first
        try:
            if locator.count() and locator.is_visible():
                locator.click(timeout=1_500)
                return
        except Exception:
            continue


def _split_page_title(page_title: str | None) -> tuple[str | None, str | None]:
    if not page_title:
        return None, None
    parts = [part.strip() for part in page_title.split("|") if part.strip()]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return (parts[0], None) if parts else (None, None)


def _fallback_extract_from_main(page) -> dict[str, Any]:
    main_text = _locator_text(page.locator("main").first)
    lines = _non_empty_lines(main_text)
    page_title_title, page_title_company = _split_page_title(page.title())

    company_link = page.locator("main a[href*='/company/']").first
    company_name = _locator_text(company_link) or page_title_company
    company_url = _locator_attr(company_link, "href")
    title = page_title_title

    location = posted_time = applicants = None
    if title and title in lines:
        idx = lines.index(title)
        for line in lines[idx + 1 : idx + 6]:
            parts = [part.strip() for part in re.split(r"[·•]", line) if part.strip()]
            if not parts:
                continue
            if not location:
                location = parts[0]
            for part in parts[1:]:
                lowered = part.lower()
                if not posted_time and any(
                    token in lowered
                    for token in ("ago", "hour", "day", "week", "month")
                ):
                    posted_time = part
                if not applicants and (
                    "applicant" in lowered or "clicked apply" in lowered
                ):
                    applicants = part
            if location or posted_time or applicants:
                break

    criteria: dict[str, str] = {}
    if title and title in lines:
        idx = lines.index(title)
        top_slice = lines[idx + 1 : idx + 12]
        employment_values = {
            "Full-time",
            "Part-time",
            "Contract",
            "Temporary",
            "Internship",
            "Volunteer",
        }
        workplace_values = {"Remote", "Hybrid", "On-site", "Onsite"}
        for line in top_slice:
            if line in workplace_values:
                criteria["Workplace type"] = line
            elif line in employment_values:
                criteria["Employment type"] = line
            elif "Promoted by" in line:
                criteria["Promotion"] = line

    description_text = None
    if "About the job" in lines:
        start = lines.index("About the job") + 1
        end_markers = {"Set alert for similar jobs", "About the company", "More jobs"}
        end = len(lines)
        for i in range(start, len(lines)):
            if lines[i] in end_markers:
                end = i
                break
        body_lines = lines[start:end]
        if body_lines:
            description_text = "\n".join(body_lines).strip()

    return {
        "title": title,
        "company_name": company_name,
        "company_url": company_url,
        "location": location,
        "posted_time": posted_time,
        "applicants": applicants,
        "description_text": description_text,
        "metadata_text": main_text,
        "criteria": criteria,
    }


def _extract_apply_links(page) -> dict[str, str | None]:
    apply_linkedin_url = None
    for candidate in _top_action_candidates(page):
        text = candidate["text"].lower()
        href = candidate["href"]
        if "apply" not in text or text == "save":
            continue
        if href:
            apply_linkedin_url = href
            break

    apply_direct_url = None
    if apply_linkedin_url:
        parsed = urlparse(apply_linkedin_url)
        target = parse_qs(parsed.query).get("url", [None])[0]
        apply_direct_url = unquote(target) if target else None

    return {
        "apply_linkedin_url": apply_linkedin_url,
        "apply_direct_url": apply_direct_url,
    }


def _extract_easy_apply(page) -> dict[str, Any]:
    easy_apply_present = False
    apply_button_text = None
    for candidate in _top_action_candidates(page):
        text = candidate["text"]
        lowered = text.lower()
        if lowered == "save":
            continue
        if "easy apply" in lowered:
            easy_apply_present = True
            apply_button_text = text
            break
        if lowered == "apply":
            apply_button_text = text
            break

    if not apply_button_text:
        top_lines = _header_lines(page)
        if "Easy Apply" in top_lines:
            easy_apply_present = True
            apply_button_text = "Easy Apply"
        elif "Apply" in top_lines:
            apply_button_text = "Apply"

    return {
        "easy_apply": easy_apply_present,
        "apply_button_text": apply_button_text,
        "apply_method": "easy_apply" if easy_apply_present else None,
    }


def _extract_application_status(page) -> dict[str, Any]:
    lines = _header_lines(page)
    header_text = " ".join(lines)
    normalized = header_text.lower()

    if "no longer accepting applications" in normalized:
        return {
            "accepting_applications": False,
            "application_status": "no_longer_accepting_applications",
        }

    if "applications closed" in normalized:
        return {
            "accepting_applications": False,
            "application_status": "applications_closed",
        }

    if "apply" in normalized or "easy apply" in normalized:
        return {
            "accepting_applications": True,
            "application_status": "accepting_applications",
        }

    return {
        "accepting_applications": None,
        "application_status": None,
    }


def scrape_linkedin_job(
    job_id_or_url: str,
    *,
    profile_dir: str | Path = DEFAULT_PROFILE_DIR,
    browser_channel: str | None = None,
    headless: bool = True,
) -> LinkedInJobDetail:
    """
    Scrape a single LinkedIn jobs/view page using an authenticated browser session.

    The caller must first create a persistent logged-in Playwright profile.
    """
    profile_dir = Path(profile_dir)
    if not profile_dir.exists():
        raise RuntimeError(
            f"Profile directory '{profile_dir}' does not exist. Create a logged-in "
            "Playwright profile before scraping LinkedIn job details."
        )

    normalized_job_id, url = normalize_linkedin_job(job_id_or_url)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel=browser_channel,
            headless=headless,
            viewport={"width": 1440, "height": 1080},
        )
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            _wait_for_job_page(page)
            if _detect_login_wall(page):
                raise RuntimeError(
                    "LinkedIn redirected to a login wall. Refresh the saved browser "
                    "profile with an authenticated LinkedIn session."
                )

            _maybe_expand_description(page)
            page.wait_for_timeout(1_500)

            title = _text_or_none(
                page,
                [
                    "h1.t-24",
                    ".job-details-jobs-unified-top-card__job-title h1",
                    ".job-details-jobs-unified-top-card__job-title",
                    ".top-card-layout__title",
                ],
            )
            company_name = _text_or_none(
                page,
                [
                    ".job-details-jobs-unified-top-card__company-name a",
                    ".job-details-jobs-unified-top-card__company-name",
                    ".jobs-unified-top-card__company-name",
                    ".topcard__org-name-link",
                ],
            )
            company_url = None
            for selector in [
                ".job-details-jobs-unified-top-card__company-name a",
                ".jobs-unified-top-card__company-name a",
            ]:
                locator = page.locator(selector).first
                try:
                    if locator.count():
                        company_url = _locator_attr(locator, "href")
                        if company_url:
                            break
                except Exception:
                    continue
            metadata_text = _text_or_none(
                page,
                [
                    ".job-details-jobs-unified-top-card__primary-description-container",
                    ".job-details-jobs-unified-top-card__tertiary-description-container",
                    ".jobs-unified-top-card__primary-description",
                    ".jobs-unified-top-card__subtitle-primary-grouping",
                ],
            )
            description_text = _text_or_none(
                page,
                [
                    ".jobs-description__content .jobs-box__html-content",
                    ".jobs-description__content",
                    ".show-more-less-html__markup",
                ],
            )

            description_html = None
            for selector in [
                ".jobs-description__content .jobs-box__html-content",
                ".jobs-description__content",
                ".show-more-less-html__markup",
            ]:
                locator = page.locator(selector).first
                try:
                    if locator.count():
                        description_html = locator.inner_html()
                        if description_html:
                            break
                except Exception:
                    continue

            criteria: dict[str, str] = {}
            skill_matches = _texts(
                page,
                [
                    ".job-details-how-you-match__skills-item-subtitle",
                    ".job-details-how-you-match__skills-item-wrapper",
                    "[aria-label='Skills'] span",
                ],
            )
            benefits = _texts(
                page,
                [".job-details-benefits__list-item", ".jobs-benefits__list-item"],
            )
            canonical_url = (
                _locator_attr(page.locator("link[rel='canonical']").first, "href")
                or page.url
            )

            fallback = _fallback_extract_from_main(page)
            apply_links = _extract_apply_links(page)
            easy_apply_info = _extract_easy_apply(page)
            application_status = _extract_application_status(page)

            title = title or fallback["title"]
            company_name = company_name or fallback["company_name"]
            company_url = company_url or fallback["company_url"]
            location = fallback["location"]
            posted_time = fallback["posted_time"]
            applicants = fallback["applicants"]
            description_text = description_text or fallback["description_text"]
            metadata_text = metadata_text or fallback["metadata_text"]
            if fallback["criteria"]:
                criteria = {**fallback["criteria"], **criteria}

            apply_method = easy_apply_info["apply_method"]
            if not apply_method and apply_links["apply_direct_url"]:
                apply_method = "external"
            elif not apply_method and apply_links["apply_linkedin_url"]:
                apply_method = "linkedin_redirect"

            return LinkedInJobDetail(
                job_id=normalized_job_id,
                job_url=canonical_url,
                title=title,
                company_name=company_name,
                company_url=company_url,
                location=location,
                posted_time=posted_time,
                applicants=applicants,
                description_text=description_text,
                description_html=description_html,
                criteria=criteria,
                skills=skill_matches,
                benefits=benefits,
                metadata_text=metadata_text,
                apply_linkedin_url=apply_links["apply_linkedin_url"],
                apply_direct_url=apply_links["apply_direct_url"],
                easy_apply=easy_apply_info["easy_apply"],
                apply_button_text=easy_apply_info["apply_button_text"],
                apply_method=apply_method,
                accepting_applications=application_status["accepting_applications"],
                application_status=application_status["application_status"],
            )
        finally:
            context.close()
