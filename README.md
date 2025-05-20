# Flask Content-API + Postgres Proxy

Proxies your React Native → 3rd-party “content-api” calls and your local SSH-tunneled Postgres.

---

## 📦 Requirements

- Python 3.8+
- pip
  pip install -r requirements.txt
  pip install python-dotenv

# Spin up

## Terminal A: public bastion → internal jump host

ssh -N -L 2222:172.31.67.208:22 joshua@35.177.249.54

## Terminal B: localhost:5432 → remote Postgres via the jump host

ssh -i ~/.ssh/id_ed25519 \
 -N -L 5432:content-api-postgres.c14a0gc6ym4o.eu-west-2.rds.amazonaws.com:5432 \
 joshua@localhost -p 2222

```
## Terminal C: Run backend locally
flask run --host=0.0.0.0 --port=4000

#### What this does:
Step A forwards your local port 2222 to the internal host’s SSH on 172.31.67.208.
Step B then uses that tunnel (localhost:2222) to forward your local port 5432 to the actual RDS Postgres.


```
