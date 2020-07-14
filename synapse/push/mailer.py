# -*- coding: utf-8 -*-
# Copyright 2016 OpenMarket Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import email.mime.multipart
import email.utils
import logging
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Iterable, List, TypeVar

from six.moves import urllib

import bleach
import jinja2

from synapse.api.constants import EventTypes
from synapse.api.errors import StoreError
from synapse.config.emailconfig import EmailSubjectConfig
from synapse.logging.context import make_deferred_yieldable
from synapse.push.presentable_names import (
    calculate_room_name,
    descriptor_from_member_events,
    name_from_member_event,
)
from synapse.types import UserID
from synapse.util.async_helpers import concurrently_execute
from synapse.visibility import filter_events_for_client

logger = logging.getLogger(__name__)

T = TypeVar("T")


CONTEXT_BEFORE = 1
CONTEXT_AFTER = 1

# From https://github.com/matrix-org/matrix-react-sdk/blob/master/src/HtmlUtils.js
ALLOWED_TAGS = [
    "font",  # custom to matrix for IRC-style font coloring
    "del",  # for markdown
    # deliberately no h1/h2 to stop people shouting.
    "h3",
    "h4",
    "h5",
    "h6",
    "blockquote",
    "p",
    "a",
    "ul",
    "ol",
    "nl",
    "li",
    "b",
    "i",
    "u",
    "strong",
    "em",
    "strike",
    "code",
    "hr",
    "br",
    "div",
    "table",
    "thead",
    "caption",
    "tbody",
    "tr",
    "th",
    "td",
    "pre",
]
ALLOWED_ATTRS = {
    # custom ones first:
    "font": ["color"],  # custom to matrix
    "a": ["href", "name", "target"],  # remote target: custom to matrix
    # We don't currently allow img itself by default, but this
    # would make sense if we did
    "img": ["src"],
}
# When bleach release a version with this option, we can specify schemes
# ALLOWED_SCHEMES = ["http", "https", "ftp", "mailto"]


class Mailer(object):
    def __init__(self, hs, app_name, template_html, template_text):
        self.hs = hs
        self.template_html = template_html
        self.template_text = template_text

        self.sendmail = self.hs.get_sendmail()
        self.store = self.hs.get_datastore()
        self.macaroon_gen = self.hs.get_macaroon_generator()
        self.state_handler = self.hs.get_state_handler()
        self.storage = hs.get_storage()
        self.app_name = app_name
        self.email_subjects = hs.config.email_subjects  # type: EmailSubjectConfig

        logger.info("Created Mailer for app_name %s" % app_name)

    async def send_password_reset_mail(self, email_address, token, client_secret, sid):
        """Send an email with a password reset link to a user

        Args:
            email_address (str): Email address we're sending the password
                reset to
            token (str): Unique token generated by the server to verify
                the email was received
            client_secret (str): Unique token generated by the client to
                group together multiple email sending attempts
            sid (str): The generated session ID
        """
        params = {"token": token, "client_secret": client_secret, "sid": sid}
        link = (
            self.hs.config.public_baseurl
            + "_matrix/client/unstable/password_reset/email/submit_token?%s"
            % urllib.parse.urlencode(params)
        )

        template_vars = {"link": link}

        await self.send_email(
            email_address,
            self.email_subjects.password_reset
            % {"server_name": self.hs.config.server_name},
            template_vars,
        )

    async def send_registration_mail(self, email_address, token, client_secret, sid):
        """Send an email with a registration confirmation link to a user

        Args:
            email_address (str): Email address we're sending the registration
                link to
            token (str): Unique token generated by the server to verify
                the email was received
            client_secret (str): Unique token generated by the client to
                group together multiple email sending attempts
            sid (str): The generated session ID
        """
        params = {"token": token, "client_secret": client_secret, "sid": sid}
        link = (
            self.hs.config.public_baseurl
            + "_matrix/client/unstable/registration/email/submit_token?%s"
            % urllib.parse.urlencode(params)
        )

        template_vars = {"link": link}

        await self.send_email(
            email_address,
            self.email_subjects.email_validation
            % {"server_name": self.hs.config.server_name},
            template_vars,
        )

    async def send_add_threepid_mail(self, email_address, token, client_secret, sid):
        """Send an email with a validation link to a user for adding a 3pid to their account

        Args:
            email_address (str): Email address we're sending the validation link to

            token (str): Unique token generated by the server to verify the email was received

            client_secret (str): Unique token generated by the client to group together
                multiple email sending attempts

            sid (str): The generated session ID
        """
        params = {"token": token, "client_secret": client_secret, "sid": sid}
        link = (
            self.hs.config.public_baseurl
            + "_matrix/client/unstable/add_threepid/email/submit_token?%s"
            % urllib.parse.urlencode(params)
        )

        template_vars = {"link": link}

        await self.send_email(
            email_address,
            self.email_subjects.email_validation
            % {"server_name": self.hs.config.server_name},
            template_vars,
        )

    async def send_notification_mail(
        self, app_id, user_id, email_address, push_actions, reason
    ):
        """Send email regarding a user's room notifications"""
        rooms_in_order = deduped_ordered_list([pa["room_id"] for pa in push_actions])

        notif_events = await self.store.get_events(
            [pa["event_id"] for pa in push_actions]
        )

        notifs_by_room = {}
        for pa in push_actions:
            notifs_by_room.setdefault(pa["room_id"], []).append(pa)

        # collect the current state for all the rooms in which we have
        # notifications
        state_by_room = {}

        try:
            user_display_name = await self.store.get_profile_displayname(
                UserID.from_string(user_id).localpart
            )
            if user_display_name is None:
                user_display_name = user_id
        except StoreError:
            user_display_name = user_id

        async def _fetch_room_state(room_id):
            room_state = await self.store.get_current_state_ids(room_id)
            state_by_room[room_id] = room_state

        # Run at most 3 of these at once: sync does 10 at a time but email
        # notifs are much less realtime than sync so we can afford to wait a bit.
        await concurrently_execute(_fetch_room_state, rooms_in_order, 3)

        # actually sort our so-called rooms_in_order list, most recent room first
        rooms_in_order.sort(key=lambda r: -(notifs_by_room[r][-1]["received_ts"] or 0))

        rooms = []

        for r in rooms_in_order:
            roomvars = await self.get_room_vars(
                r, user_id, notifs_by_room[r], notif_events, state_by_room[r]
            )
            rooms.append(roomvars)

        reason["room_name"] = await calculate_room_name(
            self.store,
            state_by_room[reason["room_id"]],
            user_id,
            fallback_to_members=True,
        )

        summary_text = await self.make_summary_text(
            notifs_by_room, state_by_room, notif_events, user_id, reason
        )

        template_vars = {
            "user_display_name": user_display_name,
            "unsubscribe_link": self.make_unsubscribe_link(
                user_id, app_id, email_address
            ),
            "summary_text": summary_text,
            "app_name": self.app_name,
            "rooms": rooms,
            "reason": reason,
        }

        await self.send_email(email_address, summary_text, template_vars)

    async def send_email(self, email_address, subject, template_vars):
        """Send an email with the given information and template text"""
        try:
            from_string = self.hs.config.email_notif_from % {"app": self.app_name}
        except TypeError:
            from_string = self.hs.config.email_notif_from

        raw_from = email.utils.parseaddr(from_string)[1]
        raw_to = email.utils.parseaddr(email_address)[1]

        if raw_to == "":
            raise RuntimeError("Invalid 'to' address")

        html_text = self.template_html.render(**template_vars)
        html_part = MIMEText(html_text, "html", "utf8")

        plain_text = self.template_text.render(**template_vars)
        text_part = MIMEText(plain_text, "plain", "utf8")

        multipart_msg = MIMEMultipart("alternative")
        multipart_msg["Subject"] = subject
        multipart_msg["From"] = from_string
        multipart_msg["To"] = email_address
        multipart_msg["Date"] = email.utils.formatdate()
        multipart_msg["Message-ID"] = email.utils.make_msgid()
        multipart_msg.attach(text_part)
        multipart_msg.attach(html_part)

        logger.info("Sending email to %s" % email_address)

        await make_deferred_yieldable(
            self.sendmail(
                self.hs.config.email_smtp_host,
                raw_from,
                raw_to,
                multipart_msg.as_string().encode("utf8"),
                reactor=self.hs.get_reactor(),
                port=self.hs.config.email_smtp_port,
                requireAuthentication=self.hs.config.email_smtp_user is not None,
                username=self.hs.config.email_smtp_user,
                password=self.hs.config.email_smtp_pass,
                requireTransportSecurity=self.hs.config.require_transport_security,
            )
        )

    async def get_room_vars(
        self, room_id, user_id, notifs, notif_events, room_state_ids
    ):
        my_member_event_id = room_state_ids[("m.room.member", user_id)]
        my_member_event = await self.store.get_event(my_member_event_id)
        is_invite = my_member_event.content["membership"] == "invite"

        room_name = await calculate_room_name(self.store, room_state_ids, user_id)

        room_vars = {
            "title": room_name,
            "hash": string_ordinal_total(room_id),  # See sender avatar hash
            "notifs": [],
            "invite": is_invite,
            "link": self.make_room_link(room_id),
        }

        if not is_invite:
            for n in notifs:
                notifvars = await self.get_notif_vars(
                    n, user_id, notif_events[n["event_id"]], room_state_ids
                )

                # merge overlapping notifs together.
                # relies on the notifs being in chronological order.
                merge = False
                if room_vars["notifs"] and "messages" in room_vars["notifs"][-1]:
                    prev_messages = room_vars["notifs"][-1]["messages"]
                    for message in notifvars["messages"]:
                        pm = list(
                            filter(lambda pm: pm["id"] == message["id"], prev_messages)
                        )
                        if pm:
                            if not message["is_historical"]:
                                pm[0]["is_historical"] = False
                            merge = True
                        elif merge:
                            # we're merging, so append any remaining messages
                            # in this notif to the previous one
                            prev_messages.append(message)

                if not merge:
                    room_vars["notifs"].append(notifvars)

        return room_vars

    async def get_notif_vars(self, notif, user_id, notif_event, room_state_ids):
        results = await self.store.get_events_around(
            notif["room_id"],
            notif["event_id"],
            before_limit=CONTEXT_BEFORE,
            after_limit=CONTEXT_AFTER,
        )

        ret = {
            "link": self.make_notif_link(notif),
            "ts": notif["received_ts"],
            "messages": [],
        }

        the_events = await filter_events_for_client(
            self.storage, user_id, results["events_before"]
        )
        the_events.append(notif_event)

        for event in the_events:
            messagevars = await self.get_message_vars(notif, event, room_state_ids)
            if messagevars is not None:
                ret["messages"].append(messagevars)

        return ret

    async def get_message_vars(self, notif, event, room_state_ids):
        if event.type != EventTypes.Message:
            return

        sender_state_event_id = room_state_ids[("m.room.member", event.sender)]
        sender_state_event = await self.store.get_event(sender_state_event_id)
        sender_name = name_from_member_event(sender_state_event)
        sender_avatar_url = sender_state_event.content.get("avatar_url")

        # 'hash' for deterministically picking default images: use
        # sender_hash % the number of default images to choose from
        sender_hash = string_ordinal_total(event.sender)

        msgtype = event.content.get("msgtype")

        ret = {
            "msgtype": msgtype,
            "is_historical": event.event_id != notif["event_id"],
            "id": event.event_id,
            "ts": event.origin_server_ts,
            "sender_name": sender_name,
            "sender_avatar_url": sender_avatar_url,
            "sender_hash": sender_hash,
        }

        if msgtype == "m.text":
            self.add_text_message_vars(ret, event)
        elif msgtype == "m.image":
            self.add_image_message_vars(ret, event)

        if "body" in event.content:
            ret["body_text_plain"] = event.content["body"]

        return ret

    def add_text_message_vars(self, messagevars, event):
        msgformat = event.content.get("format")

        messagevars["format"] = msgformat

        formatted_body = event.content.get("formatted_body")
        body = event.content.get("body")

        if msgformat == "org.matrix.custom.html" and formatted_body:
            messagevars["body_text_html"] = safe_markup(formatted_body)
        elif body:
            messagevars["body_text_html"] = safe_text(body)

        return messagevars

    def add_image_message_vars(self, messagevars, event):
        messagevars["image_url"] = event.content["url"]

        return messagevars

    async def make_summary_text(
        self, notifs_by_room, room_state_ids, notif_events, user_id, reason
    ):
        if len(notifs_by_room) == 1:
            # Only one room has new stuff
            room_id = list(notifs_by_room.keys())[0]

            # If the room has some kind of name, use it, but we don't
            # want the generated-from-names one here otherwise we'll
            # end up with, "new message from Bob in the Bob room"
            room_name = await calculate_room_name(
                self.store, room_state_ids[room_id], user_id, fallback_to_members=False
            )

            my_member_event_id = room_state_ids[room_id][("m.room.member", user_id)]
            my_member_event = await self.store.get_event(my_member_event_id)
            if my_member_event.content["membership"] == "invite":
                inviter_member_event_id = room_state_ids[room_id][
                    ("m.room.member", my_member_event.sender)
                ]
                inviter_member_event = await self.store.get_event(
                    inviter_member_event_id
                )
                inviter_name = name_from_member_event(inviter_member_event)

                if room_name is None:
                    return self.email_subjects.invite_from_person % {
                        "person": inviter_name,
                        "app": self.app_name,
                    }
                else:
                    return self.email_subjects.invite_from_person_to_room % {
                        "person": inviter_name,
                        "room": room_name,
                        "app": self.app_name,
                    }

            sender_name = None
            if len(notifs_by_room[room_id]) == 1:
                # There is just the one notification, so give some detail
                event = notif_events[notifs_by_room[room_id][0]["event_id"]]
                if ("m.room.member", event.sender) in room_state_ids[room_id]:
                    state_event_id = room_state_ids[room_id][
                        ("m.room.member", event.sender)
                    ]
                    state_event = await self.store.get_event(state_event_id)
                    sender_name = name_from_member_event(state_event)

                if sender_name is not None and room_name is not None:
                    return self.email_subjects.message_from_person_in_room % {
                        "person": sender_name,
                        "room": room_name,
                        "app": self.app_name,
                    }
                elif sender_name is not None:
                    return self.email_subjects.message_from_person % {
                        "person": sender_name,
                        "app": self.app_name,
                    }
            else:
                # There's more than one notification for this room, so just
                # say there are several
                if room_name is not None:
                    return self.email_subjects.messages_in_room % {
                        "room": room_name,
                        "app": self.app_name,
                    }
                else:
                    # If the room doesn't have a name, say who the messages
                    # are from explicitly to avoid, "messages in the Bob room"
                    sender_ids = list(
                        {
                            notif_events[n["event_id"]].sender
                            for n in notifs_by_room[room_id]
                        }
                    )

                    member_events = await self.store.get_events(
                        [
                            room_state_ids[room_id][("m.room.member", s)]
                            for s in sender_ids
                        ]
                    )

                    return self.email_subjects.messages_from_person % {
                        "person": descriptor_from_member_events(member_events.values()),
                        "app": self.app_name,
                    }
        else:
            # Stuff's happened in multiple different rooms

            # ...but we still refer to the 'reason' room which triggered the mail
            if reason["room_name"] is not None:
                return self.email_subjects.messages_in_room_and_others % {
                    "room": reason["room_name"],
                    "app": self.app_name,
                }
            else:
                # If the reason room doesn't have a name, say who the messages
                # are from explicitly to avoid, "messages in the Bob room"
                room_id = reason["room_id"]

                sender_ids = list(
                    {
                        notif_events[n["event_id"]].sender
                        for n in notifs_by_room[room_id]
                    }
                )

                member_events = await self.store.get_events(
                    [room_state_ids[room_id][("m.room.member", s)] for s in sender_ids]
                )

                return self.email_subjects.messages_from_person_and_others % {
                    "person": descriptor_from_member_events(member_events.values()),
                    "app": self.app_name,
                }

    def make_room_link(self, room_id):
        if self.hs.config.email_riot_base_url:
            base_url = "%s/#/room" % (self.hs.config.email_riot_base_url)
        elif self.app_name == "Vector":
            # need /beta for Universal Links to work on iOS
            base_url = "https://vector.im/beta/#/room"
        else:
            base_url = "https://matrix.to/#"
        return "%s/%s" % (base_url, room_id)

    def make_notif_link(self, notif):
        if self.hs.config.email_riot_base_url:
            return "%s/#/room/%s/%s" % (
                self.hs.config.email_riot_base_url,
                notif["room_id"],
                notif["event_id"],
            )
        elif self.app_name == "Vector":
            # need /beta for Universal Links to work on iOS
            return "https://vector.im/beta/#/room/%s/%s" % (
                notif["room_id"],
                notif["event_id"],
            )
        else:
            return "https://matrix.to/#/%s/%s" % (notif["room_id"], notif["event_id"])

    def make_unsubscribe_link(self, user_id, app_id, email_address):
        params = {
            "access_token": self.macaroon_gen.generate_delete_pusher_token(user_id),
            "app_id": app_id,
            "pushkey": email_address,
        }

        # XXX: make r0 once API is stable
        return "%s_matrix/client/unstable/pushers/remove?%s" % (
            self.hs.config.public_baseurl,
            urllib.parse.urlencode(params),
        )


def safe_markup(raw_html):
    return jinja2.Markup(
        bleach.linkify(
            bleach.clean(
                raw_html,
                tags=ALLOWED_TAGS,
                attributes=ALLOWED_ATTRS,
                # bleach master has this, but it isn't released yet
                # protocols=ALLOWED_SCHEMES,
                strip=True,
            )
        )
    )


def safe_text(raw_text):
    """
    Process text: treat it as HTML but escape any tags (ie. just escape the
    HTML) then linkify it.
    """
    return jinja2.Markup(
        bleach.linkify(bleach.clean(raw_text, tags=[], attributes={}, strip=False))
    )


def deduped_ordered_list(it: Iterable[T]) -> List[T]:
    seen = set()
    ret = []
    for item in it:
        if item not in seen:
            seen.add(item)
            ret.append(item)
    return ret


def string_ordinal_total(s):
    tot = 0
    for c in s:
        tot += ord(c)
    return tot


def format_ts_filter(value, format):
    return time.strftime(format, time.localtime(value / 1000))


def load_jinja2_templates(
    template_dir,
    template_filenames,
    apply_format_ts_filter=False,
    apply_mxc_to_http_filter=False,
    public_baseurl=None,
):
    """Loads and returns one or more jinja2 templates and applies optional filters

    Args:
        template_dir (str): The directory where templates are stored
        template_filenames (list[str]): A list of template filenames
        apply_format_ts_filter (bool): Whether to apply a template filter that formats
            timestamps
        apply_mxc_to_http_filter (bool): Whether to apply a template filter that converts
            mxc urls to http urls
        public_baseurl (str|None): The public baseurl of the server. Required for
            apply_mxc_to_http_filter to be enabled

    Returns:
        A list of jinja2 templates corresponding to the given list of filenames,
        with order preserved
    """
    logger.info(
        "loading email templates %s from '%s'", template_filenames, template_dir
    )
    loader = jinja2.FileSystemLoader(template_dir)
    env = jinja2.Environment(loader=loader)

    if apply_format_ts_filter:
        env.filters["format_ts"] = format_ts_filter

    if apply_mxc_to_http_filter and public_baseurl:
        env.filters["mxc_to_http"] = _create_mxc_to_http_filter(public_baseurl)

    templates = []
    for template_filename in template_filenames:
        template = env.get_template(template_filename)
        templates.append(template)

    return templates


def _create_mxc_to_http_filter(public_baseurl):
    def mxc_to_http_filter(value, width, height, resize_method="crop"):
        if value[0:6] != "mxc://":
            return ""

        serverAndMediaId = value[6:]
        fragment = None
        if "#" in serverAndMediaId:
            (serverAndMediaId, fragment) = serverAndMediaId.split("#", 1)
            fragment = "#" + fragment

        params = {"width": width, "height": height, "method": resize_method}
        return "%s_matrix/media/v1/thumbnail/%s?%s%s" % (
            public_baseurl,
            serverAndMediaId,
            urllib.parse.urlencode(params),
            fragment or "",
        )

    return mxc_to_http_filter
