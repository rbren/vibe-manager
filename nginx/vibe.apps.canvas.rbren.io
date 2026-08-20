server {
    listen 80;
    listen [::]:80;
    server_name vibe.apps.canvas.rbren.io;

    # Allow ACME HTTP-01 challenge through without auth.
    location /.well-known/acme-challenge/ {
        auth_basic off;
        root /var/www/html;
    }

    location / {
        auth_basic "Restricted";
        auth_basic_user_file /etc/nginx/.htpasswd;

        # Ticket attachment uploads (backend enforces 25 MB).
        client_max_body_size 26m;

        proxy_pass http://127.0.0.1:18300;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
