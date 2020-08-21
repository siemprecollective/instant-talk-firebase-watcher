import analytics
import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore
from firebase_admin import messaging

import apns2
from apns2.client import APNsClient
from apns2.payload import Payload

import ssl
ssl._create_default_https_context = ssl._create_unverified_context

from collections import defaultdict, namedtuple
from datetime import datetime, timezone
from time import sleep
from threading import Thread, Timer
import os
import pickle
import traceback

DEV = os.getenv("DEV", "0") == "1"
if DEV:
    print("DEVELOPMENT mode ON")
USER_COLLECTION = "new-users"
GROUP_COLLECTION = "groups"
FRIEND_REQUEST_COLLECTION = "friend-requests"
CONVERSATION_COLLECTION = "conversations"

APNS_REFRESH_INTERVAL = 60 # seconds
HEARTBEAT_INTERVAL = 120 # seconds
HEARTBEAT_TIMEOUT = 60 # seconds
NOTIFICATION_SMOTHER_INTERVAL = 10 # seconds

client_tokens = {}
client_voip_tokens = {}
users = {}

def on_error(error, items):
    print("An error occurred:", error)

analytics.write_key = "BLANK"
analytics.debug = True
analytics.on_error = on_error

cred = credentials.Certificate("BLANK")
firebase_admin.initialize_app(cred)
db = firestore.client()

update_timers = {}
def send_update_smothering(friendid, updatedid):
    timer = update_timers.get(friendid, None)
    if timer != None:
        timer.cancel()
    update_timers[friendid] = Timer(NOTIFICATION_SMOTHER_INTERVAL, send_update_notification, args=[friendid, updatedid])
    update_timers[friendid].start()
def send_update_notification(friendid, updatedid):
    if friendid not in users:
        return

    if friendid in client_tokens:
        if friendid not in client_tokens:
            return
        if "notifyPreference" not in users[friendid] or users[friendid]["notifyPreference"] != True:
            return
        token = client_tokens[friendid]
            
        friends_so_far = filter(lambda fid: fid in users, users[friendid]["friends"])
        friends_filtered = filter(lambda fid: users[fid]["status"] == 0 and users[fid].get("active", True), friends_so_far)
        friend_names = map(lambda fid: users[fid]["name"].strip(), friends_filtered)
        friend_names = list(friend_names)
      
        if len(friend_names) == 0:
            name_str = ""
        elif len(friend_names) == 1:
            name_str = friend_names[0] + " is free. "
        else:
            name_str = ", ".join(friend_names[:-1]) + " and " + friend_names[-1] + " are free. "
        if (users[friendid]["status"] == 0):
            status_str = "You are free."
        else:
            status_str = "You are busy."
        status_str = "" # TODO
        notif_str = name_str + status_str
        if notif_str == "":
            notif_str = "Everyone is busy."
        
        print("sending notification to " + users[friendid]["name"] + ": " + notif_str)
        payload = Payload(alert=notif_str)
        try:
            client = APNsClient("siempreone-push.pem", use_sandbox=DEV)
            client.send_notification(token, payload, "com.siempre.SiempreOne", collapse_id="siempre_persist", expiration=0)
        except apns2.errors.BadDeviceToken:
            print("got bad token, deleting "+ users[friendid]["name"])
            # del client_tokens[friendid]
        except apns2.errors.TopicDisallowed:
            print("got bad token, deleting "+ users[friendid]["name"])
            # del client_tokens[friendid]
    if "FCMToken" in users[friendid] and not DEV:
        if updatedid not in users:
            return
        token = users[friendid]["FCMToken"]
        message = messaging.Message(
            data={
                'type': 'status_change',
                'status': str(users[updatedid]["status"]),
                'name': users[updatedid]["name"]
            },
            token=token
        )
        response = messaging.send(message)
        print('Successfully sent message:', response)

def send_friend_request_notification(to_id, from_id):
    if from_id not in users or to_id not in users:
        return

    if "FCMToken" in users[to_id] and not DEV:
        token = users[to_id]["FCMToken"]
        if token is None:
            return
        print('Sending friend request notif from {} to {} with token {}'.format(from_id, to_id, token))
        message = messaging.Message(
            data={
                'type': 'friend_request',
                'from_id': from_id
            },
            token=token
        )
        response = messaging.send(message)
        print('Successfully sent message:', response)
    if to_id in client_tokens:
        if "name" not in users[from_id] or "name" not in users[to_id]:
            return

        print('Sending friend request notif from {} to {}'.format(users[from_id]["name"], users[to_id]["name"]))
        token = client_tokens[to_id]
        notif_str = "{} sent you a friend request!".format(users[from_id]["name"])
        payload = Payload(alert=notif_str)
        try:
            client = APNsClient("siempreone-push.pem", use_sandbox=DEV)
            client.send_notification(token, payload, "com.siempre.SiempreOne")
        except apns2.errors.BadDeviceToken:
            print("got bad token, deleting "+ users[to_id]["name"])
            # del client_tokens[to_id]
        except apns2.errors.TopicDisallowed:
            print("got bad token, deleting "+ users[to_id]["name"])
            # del client_tokens[to_id]

def evaluate_timeout(userid):
    user = users[userid]
    now = datetime.now(timezone.utc)
    if "heartbeat" in user and isinstance(user["heartbeat"], datetime):
        if (now - user["heartbeat"]).total_seconds() > HEARTBEAT_TIMEOUT:
            if user.get("active", True):
                db.collection(USER_COLLECTION).document(userid).update({
                    "active": False
                })
                print(user["name"] + " timed out")
last_refresh = datetime.min.replace(tzinfo=timezone.utc)
def send_refresh_notifications(userids):
    global last_refresh
    notifications = []
    for userid in userids:
        if userid not in users:
            continue
        user = users[userid]
        now = datetime.now(timezone.utc)
        if userid not in client_voip_tokens:
            continue
        token = client_voip_tokens[userid]
        if (now - last_refresh).total_seconds() > HEARTBEAT_INTERVAL:
            print("asking for heartbeat")
            payload = Payload(alert="placeholder", custom={"heartbeat": ""})
            last_refresh = now
            Timer(HEARTBEAT_TIMEOUT, evaluate_timeout, args=[userid]).start()
        else:
            payload = Payload(alert="placeholder")
        Notification = namedtuple('Notification', ['token', 'payload'])
        notifications.append(Notification(token=token, payload=payload))
        print("sending refresh to " + users[userid]["name"])
    
    try:
        client = APNsClient("siempreone-push.pem", use_sandbox=DEV)
        client.send_notification_batch(notifications=notifications, topic="com.siempre.SiempreOne.voip")
    except apns2.errors.BadDeviceToken:
        print("got bad voip token")
        # del client_voip_tokens[userid]
    except apns2.errors.TopicDisallowed:
        print("got bad voip token")
        # del client_voip_tokens[userid]
    print("sent refreshes")

def send_voice_update_notification(friendid, user_name):
    if friendid not in client_voip_tokens or friendid not in users:
        return
    if "voiceNotifyPreference" not in users[friendid] or users[friendid]["voiceNotifyPreference"] != True:
        return
    token = client_voip_tokens[friendid]

    print("sending voice update to " + users[friendid]["name"])
    payload = Payload(alert="placeholder", custom={"friend-available": user_name})
    try:
        client = APNsClient("siempreone-push.pem", use_sandbox=DEV)
        client.send_notification(token, payload, "com.siempre.SiempreOne.voip")
    except apns2.errors.BadDeviceToken:
        print("got bad voip token, deleting "+ users[friendid]["name"])
        # del client_voip_tokens[friendid]
    except apns2.errors.TopicDisallowed:
        print("got bad voip token, deleting "+ users[friendid]["name"])
        # del client_voip_tokens[friendid]

friend_requests_first_run = True
def resolve_friend_requests(docs, changes, read_time):
    global friend_requests_first_run
    for change in changes:
        try:
            if (change.type.name == "ADDED" or change.type.name == "MODIFIED"):
                friend_request_info = change.document.to_dict()
                from_id = friend_request_info["from"]
                to_id = friend_request_info["to"]
                print("friend request", from_id, "->", to_id)
                existing_request = db.collection(FRIEND_REQUEST_COLLECTION)\
                                     .where("to", "==", from_id)\
                                     .where("from", "==", to_id).get()

                has_existing = False
                for request in existing_request:
                    has_existing = True
                    if DEV:
                        break
                    conversation_id = from_id + "_" + to_id if from_id < to_id else to_id + "_" + from_id
                    batch = db.batch()
                    batch.set(db.collection(CONVERSATION_COLLECTION).document(conversation_id), {
                        "users": {from_id: True, to_id: True}
                    })
                    batch.delete(db.collection(FRIEND_REQUEST_COLLECTION).document(change.document.id))
                    batch.delete(db.collection(FRIEND_REQUEST_COLLECTION).document(request.id))
                    batch.update(db.collection(USER_COLLECTION).document(from_id), {"friends." + to_id: ""})
                    batch.update(db.collection(USER_COLLECTION).document(to_id), {"friends." + from_id: ""})

                    # sorting alphabetically should not be necessary
                    conversation_id = min(from_id, to_id) + "_" + max(from_id, to_id)
                    convo_dict = {from_id: True, to_id: True}
                    batch.set(db.collection(CONVERSATION_COLLECTION).document(conversation_id), {'users': convo_dict})

                    batch.commit()
                    print("friend request satisfied")

                if not has_existing and not friend_requests_first_run:
                    send_friend_request_notification(to_id, from_id)
        except Exception as e:
            print(traceback.print_exc())
    friend_requests_first_run = False

def watch_users(docs, changes, read_time):
    for change in changes:
        try:
            if (change.type.name == "MODIFIED" or change.type.name == "ADDED"):
                user = change.document.to_dict()
                userid = change.document.id
                olduser = users.get(userid, None)
                users[userid] = user
                if DEV == user.get("APNSDev", False):
                    if "FCMToken" in user:
                        print("new Android user", user["name"], "tracking their FCM Token")
                    if "APNSPushToken" in user and (userid not in client_tokens or user["APNSPushToken"] != client_tokens[userid]):
                        print("new iOS user:", user["name"], "tracking their APNS Token")
                        client_tokens[userid] = user["APNSPushToken"]  
                    if "APNSVoIPToken" in user and (userid not in client_voip_tokens or user["APNSVoIPToken"] != client_voip_tokens[userid]):
                        print("new iOS VoIP user:", user["name"], "tracking their APNS Token")
                        client_voip_tokens[userid] = user["APNSVoIPToken"]  
                analytics.identify(userid, {"name": user["name"]})
                if olduser is not None and olduser["status"] != user["status"] and (user["status"] == 0 or user["status"] == 1):
                    print("status change:", read_time, userid, user["name"], user["status"])
                    for friendid in user["friends"]:
                        send_update_smothering(friendid, userid)
                        if user["status"] == 0:
                            send_voice_update_notification(friendid, user["name"])
            if (change.type.name == "MODIFIED" and not DEV):
                analytics.track(userid, "status changed", {
                    "status": user["status"]
                })
        except Exception as e:
            print(traceback.print_exc())

def refresh_apns_devices():
    try:
        send_refresh_notifications(list(client_voip_tokens))
    except Exception as e:
        print(traceback.print_exc())
    Timer(APNS_REFRESH_INTERVAL, refresh_apns_devices).start()

def watch_watch():
    global friend_requests_first_run
    friend_watch = db.collection(FRIEND_REQUEST_COLLECTION).on_snapshot(resolve_friend_requests)
    user_watch = db.collection(USER_COLLECTION).on_snapshot(watch_users)
    while True:
        if user_watch._closed:
            user_watch = db.collection(USER_COLLECTION).on_snapshot(watch_users)
        if friend_watch._closed:
            friend_requests_first_run = True
            friend_watch = db.collection(FRIEND_REQUEST_COLLECTION).on_snapshot(resolve_friend_requests)
        sleep(1)

Timer(APNS_REFRESH_INTERVAL, refresh_apns_devices).start()
watch_watch()
