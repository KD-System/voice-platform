// MongoDB — создание индексов для коллекции transcriptions
// Запускать: mongosh voice_platform < 002_mongo_indexes.js

db.transcriptions.createIndex({ "call_id": 1 }, { unique: true });
db.transcriptions.createIndex({ "started_at": -1 });
db.transcriptions.createIndex({ "metadata.language": 1 });
db.transcriptions.createIndex({ "segments.text": "text" });

// Шардирование по call_id (для кластерного режима)
// sh.shardCollection("voice_platform.transcriptions", { "call_id": "hashed" });

print("MongoDB indexes created for voice_platform.transcriptions");
