# Standard library
import hashlib
import hmac
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone

# Packages
import dateutil.parser
import feedparser
import flask
import gnupg
import pytz
import talisker.requests
import yaml
from ubuntu_release_info.data import Data
from canonicalwebteam.blog import BlogViews
from canonicalwebteam.blog.flask import build_blueprint
from canonicalwebteam.store_api.stores.snapstore import SnapStore
from canonicalwebteam.launchpad import Launchpad
from geolite2 import geolite2
from requests.exceptions import HTTPError


# Local
from webapp.login import empty_session, user_info
from webapp.advantage import AdvantageContracts


ip_reader = geolite2.reader()
session = talisker.requests.get_session()
store_api = SnapStore(session=talisker.requests.get_session())


def download_thank_you(category):
    context = {"http_host": flask.request.host}

    version = flask.request.args.get("version", "")
    versionPatch = flask.request.args.get("versionPatch", "")
    architecture = flask.request.args.get("architecture", "")

    # Sanitise for paths
    # (https://bugs.launchpad.net/ubuntu-website-content/+bug/1586361)
    version_pattern = re.compile(r"(\d+(?:\.\d+)+).*")
    architecture = architecture.replace("..", "")
    architecture = architecture.replace("/", "+").replace(" ", "+")

    if architecture and version_pattern.match(version):
        context["start_download"] = version and architecture
        context["version"] = version
        context["versionPatch"] = versionPatch
        context["architecture"] = architecture

    # Add mirrors
    mirrors_path = os.path.join(os.getcwd(), "etc/ubuntu-mirrors-rss.xml")

    try:
        with open(mirrors_path) as rss:
            mirrors = feedparser.parse(rss.read()).entries
    except IOError:
        mirrors = []

    # Check country code
    country_code = "NO_COUNTRY_CODE"
    ip_location = ip_reader.get(
        flask.request.headers.get("X-Real-IP", flask.request.remote_addr)
    )
    mirror_list = []

    if ip_location and "country" in ip_location:
        country_code = ip_location["country"]["iso_code"]

        mirror_list = [
            {"link": mirror["link"], "bandwidth": mirror["mirror_bandwidth"]}
            for mirror in mirrors
            if mirror["mirror_countrycode"] == country_code
            and mirror["link"].startswith("https")
        ]
    context["mirror_list"] = json.dumps(mirror_list)

    return (
        flask.render_template(
            f"download/{category}/thank-you.html", **context
        ),
        {"Cache-Control": "no-cache"},
    )


def appliance_install(app, device):
    with open("appliances.yaml") as appliances:
        appliances = yaml.load(appliances, Loader=yaml.FullLoader)

    return flask.render_template(
        f"appliance/{app}/{device}.html",
        http_host=flask.request.host,
        appliance=appliances["appliances"][app],
    )


def releasenotes_redirect():
    """
    View to redirect to https://wiki.ubuntu.com/ URLs for release notes.

    This used to be done in the Apache frontend, but that is going away
    to be replace by the content-cache.

    Old apache redirects: https://pastebin.canonical.com/p/3TXyyNkWkg/
    """

    version = flask.request.args.get("ver", "")[:5]

    for codename, release in Data().releases.items():
        short_version = ".".join(release.version.split(".")[:2])
        if version == short_version:
            release_slug = release.full_codename.replace(" ", "")

            return flask.redirect(
                f"https://wiki.ubuntu.com/{release_slug}/ReleaseNotes"
            )

    return flask.redirect("https://wiki.ubuntu.com/Releases")


def build():
    """
    Show build page
    """

    return flask.render_template(
        "core/build/index.html",
        board_architectures=json.dumps(Launchpad.board_architectures),
    )


def post_build():
    """
    Once they submit the build form on /core/build,
    kick off the build with Launchpad
    """

    opt_in = flask.request.values.get("canonicalUpdatesOptIn")
    full_name = flask.request.values.get("FullName")
    names = full_name.split(" ")
    email = flask.request.values.get("Email")
    board = flask.request.values.get("board")
    system = flask.request.values.get("system")
    snaps = flask.request.values.get("snaps", "").split(",")

    if not user_info(flask.session):
        flask.abort(401)

    launchpad = Launchpad(
        username="imagebuild",
        token=os.environ["LAUNCHPAD_TOKEN"],
        secret=os.environ["LAUNCHPAD_SECRET"],
        session=session,
        auth_consumer="image.build",
    )

    context = {}

    # Submit user to marketo
    session.post(
        "https://pages.ubuntu.com/index.php/leadCapture/save",
        data={
            "canonicalUpdatesOptIn": opt_in,
            "FirstName": " ".join(names[:-1]),
            "LastName": names[-1] if len(names) > 1 else "",
            "Email": email,
            "formid": "3546",
            "lpId": "2154",
            "subId": "30",
            "munchkinId": "066-EOV-335",
            "imageBuilderStatus": "NULL",
        },
    )

    # Ensure webhook is created
    if flask.request.host == "ubuntu.com":
        launchpad.create_update_system_build_webhook(
            system=system,
            delivery_url="https://ubuntu.com/core/build/notify",
            secret=flask.current_app.config["SECRET_KEY"],
        )

    # Kick off image build
    try:
        response = launchpad.build_image(
            board=board,
            system=system,
            snaps=snaps,
            author_info={"name": full_name, "email": email, "board": board},
            gpg_passphrase=flask.current_app.config["SECRET_KEY"],
        )
        context["build_info"] = launchpad.session.get(
            response.headers["Location"]
        ).json()
    except HTTPError as http_error:
        if http_error.response.status_code == 400:
            return (
                flask.render_template(
                    "core/build/error.html",
                    build_error=http_error.response.content.decode(),
                ),
                400,
            )
        else:
            raise http_error

    return flask.render_template("core/build/index.html", **context)


def notify_build():
    """
    An endpoint to trigger an update about a build event to be sent.
    This will usually be triggered by a webhook from Launchpad
    """

    # Verify contents
    signature = hmac.new(
        flask.current_app.config["SECRET_KEY"].encode("utf-8"),
        flask.request.data,
        hashlib.sha1,
    ).hexdigest()

    if "X-Hub-Signature" not in flask.request.headers:
        return "No X-Hub-Signature provided\n", 403

    if not hmac.compare_digest(
        signature, flask.request.headers["X-Hub-Signature"].split("=")[1]
    ):
        try:
            raise HTTPError(400)
        except HTTPError:
            flask.current_app.extensions["sentry"].captureException(
                extra={
                    "request_headers": str(flask.request.headers.keys()),
                    "message": "x-hub-signature did not match",
                    "expected_signature": signature,
                    "header_contents": flask.request.headers[
                        "X-Hub-Signature"
                    ],
                    "extracted_signature": flask.request.headers[
                        "X-Hub-Signature"
                    ].split("=")[1],
                }
            )

        return "X-Hub-Signature does not match\n", 400

    event_content = flask.request.json
    status = event_content["status"]
    build_url = (
        "https://api.launchpad.net/devel" + event_content["livefs_build"]
    )

    launchpad = Launchpad(
        username="imagebuild",
        token=os.environ["LAUNCHPAD_TOKEN"],
        secret=os.environ["LAUNCHPAD_SECRET"],
        session=session,
        auth_consumer="image.build",
    )

    build = launchpad.request(build_url).json()
    author_json = (
        gnupg.GPG()
        .decrypt(
            build["metadata_override"]["_author_data"],
            passphrase=flask.current_app.config["SECRET_KEY"],
        )
        .data
    )

    if author_json:
        author = json.loads(author_json)
    else:
        return "_author_data could not be decoded\n", 400

    email = author["email"]
    names = author["name"].split(" ")
    board = author["board"]
    snaps = ", ".join(build["metadata_override"]["extra_snaps"])
    codename = build["distro_series_link"].split("/")[-1]
    version = Data().by_codename(codename).version
    arch = build["distro_arch_series_link"].split("/")[-1]
    build_link = build["web_link"]
    build_id = build_link.split("/")[-1]

    download_url = None

    if status == "Successfully built":
        download_url = launchpad.request(
            f"{build_url}?ws.op=getFileUrls"
        ).json()[0]

    session.post(
        "https://pages.ubuntu.com/index.php/leadCapture/save",
        data={
            "FirstName": " ".join(names[:-1]),
            "LastName": names[-1] if len(names) > 1 else "",
            "Email": email,
            "formid": "3546",
            "lpId": "2154",
            "subId": "30",
            "munchkinId": "066-EOV-335",
            "imageBuilderVersion": version,
            "imageBuilderArchitecture": arch,
            "imageBuilderBoard": board,
            "imageBuilderSnaps": snaps,
            "imageBuilderID": build_id,
            "imageBuilderBuildlink": build_link,
            "imageBuilderStatus": status,
            "imageBuilderDownloadlink": download_url,
        },
    )

    return "Submitted\n", 202


def search_snaps():
    """
    A JSON endpoint to search the snap store API
    """

    query = flask.request.args.get("q", "")
    architecture = flask.request.args.get("architecture", "wide")
    board = flask.request.args.get("board")
    system = flask.request.args.get("system")
    size = flask.request.args.get("size", "100")
    page = flask.request.args.get("page", "1")

    if board and system:
        architecture = Launchpad.board_architectures[board][system]["arch"]

    if not query:
        return flask.jsonify({"error": "Query parameter 'q' empty"}), 400

    search_response = store_api.search(
        query, size=size, page=page, arch=architecture
    )

    return flask.jsonify(
        {
            "results": search_response.get("_embedded", {}).get(
                "clickindex:package", {}
            ),
            "architecture": architecture,
        }
    )


def advantage_view():
    accounts = None
    personal_account = None
    enterprise_contracts = {}
    entitlements = {}
    open_subscription = flask.request.args.get("subscription", None)
    stripe_publishable_key = os.getenv(
        "STRIPE_PUBLISHABLE_KEY", "pk_live_68aXqowUeX574aGsVck8eiIE"
    )

    if user_info(flask.session):
        advantage = AdvantageContracts(
            session,
            flask.session["authentication_token"],
            api_url=flask.current_app.config["CONTRACTS_API_URL"],
        )

        try:
            accounts = advantage.get_accounts()
        except HTTPError as http_error:
            if http_error.response.status_code == 401:
                # We got an unauthorized request, so we likely
                # need to re-login to refresh the macaroon
                flask.current_app.extensions["sentry"].captureException(
                    extra={
                        "session_keys": flask.session.keys(),
                        "request_url": http_error.request.url,
                        "request_headers": http_error.request.headers,
                        "response_headers": http_error.response.headers,
                        "response_body": http_error.response.json(),
                        "response_code": http_error.response.json()["code"],
                        "response_message": http_error.response.json()[
                            "message"
                        ],
                    }
                )

                empty_session(flask.session)

                return flask.render_template("advantage/index.html")

            raise http_error

        for account in accounts:
            account["contracts"] = advantage.get_account_contracts(account)

            for contract in account["contracts"]:
                contract["token"] = advantage.get_contract_token(contract)

                machines = advantage.get_contract_machines(contract).get(
                    "machines"
                )
                contract["machineCount"] = 0

                if machines:
                    contract["machineCount"] = len(machines)

                if contract["contractInfo"].get("origin", "") == "free":
                    personal_account = account
                    personal_account["free_token"] = contract["token"]
                    for entitlement in contract["contractInfo"][
                        "resourceEntitlements"
                    ]:
                        if entitlement["type"] == "esm-infra":
                            entitlements["esm-infra"] = True
                        elif entitlement["type"] == "esm-apps":
                            entitlements["esm-apps"] = True
                        elif entitlement["type"] == "livepatch":
                            entitlements["livepatch"] = True
                        elif entitlement["type"] == "fips":
                            entitlements["fips"] = True
                        elif entitlement["type"] == "cc-eal":
                            entitlements["cc-eal"] = True
                    personal_account["entitlements"] = entitlements
                else:
                    entitlements = {}
                    for entitlement in contract["contractInfo"][
                        "resourceEntitlements"
                    ]:
                        contract["supportLevel"] = "-"
                        if entitlement["type"] == "esm-infra":
                            entitlements["esm-infra"] = True
                        elif entitlement["type"] == "esm-apps":
                            entitlements["esm-apps"] = True
                        elif entitlement["type"] == "livepatch":
                            entitlements["livepatch"] = True
                        elif entitlement["type"] == "fips":
                            entitlements["fips"] = True
                        elif entitlement["type"] == "cc-eal":
                            entitlements["cc-eal"] = True
                        elif entitlement["type"] == "support":
                            contract["supportLevel"] = entitlement[
                                "affordances"
                            ]["supportLevel"]
                    contract["entitlements"] = entitlements
                    created_at = dateutil.parser.parse(
                        contract["contractInfo"]["createdAt"]
                    )
                    contract["contractInfo"][
                        "createdAtFormatted"
                    ] = created_at.strftime("%d %B %Y")
                    contract["contractInfo"]["status"] = "active"

                    if "effectiveTo" in contract["contractInfo"]:
                        effective_to = dateutil.parser.parse(
                            contract["contractInfo"]["effectiveTo"]
                        )
                        contract["contractInfo"][
                            "effectiveToFormatted"
                        ] = effective_to.strftime("%d %B %Y")

                        time_now = datetime.utcnow().replace(tzinfo=pytz.utc)

                        if effective_to < time_now:
                            contract["contractInfo"]["status"] = "expired"
                        else:
                            date_difference = effective_to - time_now
                            contract["expiring"] = date_difference.days <= 30
                            contract["contractInfo"][
                                "daysTillExpiry"
                            ] = date_difference.days

                    contract["renewal"] = make_renewal(
                        advantage, contract["contractInfo"]
                    )

                    enterprise_contract = enterprise_contracts.setdefault(
                        contract["accountInfo"]["name"], []
                    )
                    # If a subscription id is present and this contract
                    # matches add it to the start of the list
                    if contract["contractInfo"]["id"] == open_subscription:
                        enterprise_contract.insert(0, contract)
                    else:
                        enterprise_contract.append(contract)

    return flask.render_template(
        "advantage/index.html",
        accounts=accounts,
        enterprise_contracts=enterprise_contracts,
        personal_account=personal_account,
        open_subscription=open_subscription,
        stripe_publishable_key=stripe_publishable_key,
    )


def post_advantage_subscriptions():
    # 500 if 'error' arg is provided
    if flask.request.args.get("error"):
        return flask.jsonify({}), 500

    payload = flask.request.json
    if not payload:
        return flask.jsonify({}), 400

    customer_id = payload.get("customer_id")
    products = payload.get("products")

    if not customer_id or not products:
        return flask.jsonify({}), 400

    return flask.jsonify({}), 200


def advantage_shop_view():
    stripe_publishable_key = os.getenv(
        "STRIPE_PUBLISHABLE_KEY", "pk_live_68aXqowUeX574aGsVck8eiIE"
    )

    return flask.render_template(
        "advantage/subscribe.html",
        stripe_publishable_key=stripe_publishable_key,
    )


def make_renewal(advantage, contract_info):
    """Return the renewal as present in the given info, or None."""
    renewals = contract_info.get("renewals")
    if not renewals:
        return None

    renewal = renewals[0]

    # If the renewal is processing, we need to find out
    # whether payment failed and requires user action,
    # which is information only available in the fuller
    # renewal object get_renewal gives us.
    if renewal["status"] == "processing":
        renewal = advantage.get_renewal(renewal["id"])

    renewal["renewable"] = False

    if renewal["status"] == "done":
        try:
            renewal_modified_date = dateutil.parser.parse(
                renewal["lastModified"]
            )
            oneHourAgo = datetime.now(timezone.utc) - timedelta(hours=1)

            renewal["recently_renewed"] = oneHourAgo < renewal_modified_date
        except KeyError:
            renewal["recently_renewed"] = False

    # Only actionable renewals are renewable.
    # If "actionable" isn't set, it's not actionable
    # If "actionable" IS set, but not true, it's not actionable
    if "actionable" not in renewal:
        renewal["actionable"] = False
        return renewal
    elif not renewal["actionable"]:
        return renewal

    # The renewal is renewable only during its time window.
    start = dateutil.parser.parse(renewal["start"])
    end = dateutil.parser.parse(renewal["end"])
    if not (start <= datetime.now(timezone.utc) <= end):
        return renewal

    # Pending renewals are renewable.
    if renewal["status"] == "pending":
        renewal["renewable"] = True
        return renewal

    # Renewals not pending or processing are never renewable.
    if renewal["status"] != "processing":
        return renewal

    invoices = renewal.get("stripeInvoices")
    if invoices:
        invoice = invoices[-1]
        renewal["renewable"] = (
            invoice["pi_status"] == "requires_payment_method"
            or invoice["pi_status"] == "requires_action"
        ) and invoice["subscription_status"] == "incomplete"

    return renewal


def post_customer_info():
    if user_info(flask.session):
        advantage = AdvantageContracts(
            session,
            flask.session["authentication_token"],
            api_url=flask.current_app.config["CONTRACTS_API_URL"],
        )

        if not flask.request.is_json:
            return flask.jsonify({"error": "JSON required"}), 400

        payment_method_id = flask.request.json.get("payment_method_id")
        if not payment_method_id:
            return flask.jsonify({"error": "payment_method_id required"}), 400

        account_id = flask.request.json.get("account_id")
        if not account_id:
            return flask.jsonify({"error": "account_id required"}), 400

        address = flask.request.json.get("address")
        name = flask.request.json.get("name")
        tax_id = flask.request.json.get("tax_id")

        return advantage.put_customer_info(
            account_id, payment_method_id, address, name, tax_id
        )
    else:
        return flask.jsonify({"error": "authentication required"}), 401


def post_stripe_invoice_id(renewal_id, invoice_id):
    if user_info(flask.session):
        advantage = AdvantageContracts(
            session,
            flask.session["authentication_token"],
            api_url=flask.current_app.config["CONTRACTS_API_URL"],
        )

        return advantage.post_stripe_invoice_id(invoice_id, renewal_id)
    else:
        return flask.jsonify({"error": "authentication required"}), 401


def get_renewal(renewal_id):
    if user_info(flask.session):
        advantage = AdvantageContracts(
            session,
            flask.session["authentication_token"],
            api_url=flask.current_app.config["CONTRACTS_API_URL"],
        )

        return advantage.get_renewal(renewal_id)
    else:
        return flask.jsonify({"error": "authentication required"}), 401


def accept_renewal(renewal_id):
    if user_info(flask.session):
        advantage = AdvantageContracts(
            session,
            flask.session["authentication_token"],
            api_url=flask.current_app.config["CONTRACTS_API_URL"],
        )

        return advantage.accept_renewal(renewal_id)
    else:
        return flask.jsonify({"error": "authentication required"}), 401


def build_tutorials_index(tutorials_docs):
    def tutorials_index():
        page = flask.request.args.get("page", default=1, type=int)
        topic = flask.request.args.get("topic", default=None, type=str)
        sort = flask.request.args.get("sort", default=None, type=str)
        posts_per_page = 15
        tutorials_docs.parser.parse()
        if not topic:
            metadata = tutorials_docs.parser.metadata
        else:
            metadata = [
                doc
                for doc in tutorials_docs.parser.metadata
                if topic in doc["categories"]
            ]

        if sort == "difficulty-desc":
            metadata = sorted(
                metadata, key=lambda k: k["difficulty"], reverse=True
            )

        if sort == "difficulty-asc" or not sort:
            metadata = sorted(
                metadata, key=lambda k: k["difficulty"], reverse=False
            )

        total_pages = math.ceil(len(metadata) / posts_per_page)

        return flask.render_template(
            "tutorials/index.html",
            navigation=tutorials_docs.parser.navigation,
            forum_url=tutorials_docs.parser.api.base_url,
            metadata=metadata,
            page=page,
            topic=topic,
            sort=sort,
            posts_per_page=posts_per_page,
            total_pages=total_pages,
        )

    return tutorials_index


# Blog
# ===

blog_views = BlogViews(excluded_tags=[3184, 3265, 3408], per_page=11)
blog_blueprint = build_blueprint(blog_views)


def blog_custom_topic(slug):
    page_param = flask.request.args.get("page", default=1, type=int)
    context = blog_views.get_topic(slug, page_param)

    return flask.render_template(f"blog/topics/{slug}.html", **context)


def blog_custom_group(slug):
    page_param = flask.request.args.get("page", default=1, type=int)
    category_param = flask.request.args.get("category", default="", type=str)
    context = blog_views.get_group(slug, page_param, category_param)

    return flask.render_template(f"blog/{slug}.html", **context)


def blog_press_centre():
    page_param = flask.request.args.get("page", default=1, type=int)
    category_param = flask.request.args.get("category", default="", type=str)
    context = blog_views.get_group(
        "canonical-announcements", page_param, category_param
    )

    return flask.render_template("blog/press-centre.html", **context)
