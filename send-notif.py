import apns2
from apns2.client import APNsClient
from apns2.payload import Payload

client = APNsClient("apns.pem", use_sandbox=False)
token = "BLANK"

payload = Payload(alert="placeholder for data reload", sound="default", badge=1)
client.send_notification(token, payload, "com.siempre.SiempreOne.voip")
