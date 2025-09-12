#!/bin/bash

set -e
# Production instances are slow with date command and stderr
# export PS4='+ $(date "+%Y-%m-%d %H:%M:%S") '
# set -x

echo "TASK: Entrypoint script started"
# export MONGO_DB_DATA=$PYTHAGORA_DATA_DIR/mongodata
# mkdir -p $MONGO_DB_DATA

# # Start MongoDB in the background
# mongod --dbpath "$MONGO_DB_DATA" --bind_ip_all >> $MONGO_DB_DATA/mongo_logs.txt 2>&1 &

# # Loop until MongoDB is running (use pgrep for speed)
# for ((i=0; i<10*5; i++)); do
#   if pgrep -x mongod > /dev/null; then
#     echo "TASK: MongoDB started"
#     break
#   fi
#   sleep 0.2
# done

export DB_DIR=$PYTHAGORA_DATA_DIR/database

chown -R devuser: $PYTHAGORA_DATA_DIR
su -c "mkdir -p $DB_DIR" devuser

# Start the VS Code extension installer/HTTP server script in the background
su -c "cd /var/init_data/ && ./on-event-extension-install.sh" devuser

# Keep container running
echo "FINISH: Entrypoint script finished"
tail -f /dev/null
