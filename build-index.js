// build-index.js
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

// Ключ шифрования (тот же что на клиенте)
const KEY = process.env.XOR_KEY;

// XOR шифрование
function encrypt(text) {
  let result = '';
  for (let i = 0; i < text.length; i++) {
    result += String.fromCharCode(
      text.charCodeAt(i) ^ KEY.charCodeAt(i % KEY.length)
    );
  }
  return Buffer.from(result).toString('base64');
}

// Расшифровка
function decrypt(encoded) {
  const decoded = Buffer.from(encoded, 'base64').toString();
  let result = '';
  for (let i = 0; i < decoded.length; i++) {
    result += String.fromCharCode(
      decoded.charCodeAt(i) ^ KEY.charCodeAt(i % KEY.length)
    );
  }
  return result;
}

// Парсинг плоского массива обратно в объект
function parseMovie(arr, baseIndex) {
  return {
    id: arr[baseIndex],
    title: arr[baseIndex + 1],
    date: arr[baseIndex + 3],
    genres: arr[baseIndex + 5],
    actresses: arr[baseIndex + 6]
  };
}

// Сборка индекса
async function buildIndex() {
  const dataDir = path.join(__dirname, '..', 'data');
  const indexDir = path.join(__dirname, '..', 'index');
  
  // Создаём папку для индекса если нет
  if (!fs.existsSync(indexDir)) {
    fs.mkdirSync(indexDir, { recursive: true });
  }
  
  // Читаем все .bin файлы
  const files = fs.readdirSync(dataDir).filter(f => f.endsWith('.bin'));
  
  console.log(`Найдено ${files.length} файлов для индексации`);
  
  const indexData = [];
  let totalMovies = 0;
  
  for (const file of files) {
    const filePath = path.join(dataDir, file);
    const encrypted = fs.readFileSync(filePath, 'utf8');
    
    try {
      // Расшифровываем
      const decrypted = decrypt(encrypted);
      const movies = JSON.parse(decrypted);
      
      // Извлекаем только нужные поля (7 элементов на фильм)
      for (let i = 0; i < movies.length; i += 7) {
        indexData.push(
          movies[i],      // id
          movies[i + 1],  // title
          movies[i + 3],  // date (только дата)
          movies[i + 5],  // genres (строка)
          movies[i + 6]   // actresses (строка)
        );
        totalMovies++;
      }
      
      console.log(`  ${file}: ${movies.length / 7} фильмов`);
      
    } catch (err) {
      console.error(`  Ошибка в ${file}: ${err.message}`);
    }
  }
  
  // Сохраняем индекс
  const jsonString = JSON.stringify(indexData);
  const encrypted = encrypt(jsonString);
  
  fs.writeFileSync(path.join(indexDir, 'index.bin'), encrypted);
  
  // Статистика
  const stats = fs.statSync(path.join(indexDir, 'index.bin'));
  const sizeKB = (stats.size / 1024).toFixed(2);
  
  console.log(`\n✅ Индекс готов:`);
  console.log(`   Фильмов: ${totalMovies}`);
  console.log(`   Размер: ${sizeKB} KB`);
  console.log(`   Элементов: ${indexData.length}`);
  
  // Сохраняем метаданные индекса
  const meta = {
    lastBuild: new Date().toISOString(),
    totalMovies,
    filesCount: files.length,
    sizeBytes: stats.size
  };
  
  fs.writeFileSync(
    path.join(indexDir, 'meta.json'),
    JSON.stringify(meta, null, 2)
  );
}

buildIndex().catch(console.error);
