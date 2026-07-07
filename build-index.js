// build-index.js
const fs = require('fs');
const path = require('path');

const KEY = process.env.XOR_KEY || 'MyM0v1eK3y';

function xor(data) {
  let result = '';
  for (let i = 0; i < data.length; i++) {
    result += String.fromCharCode(
      data.charCodeAt(i) ^ KEY.charCodeAt(i % KEY.length)
    );
  }
  return result;
}

async function buildIndex() {
  const rootDir = __dirname;
  const dataDir = path.join(rootDir, 'data');
  const indexDir = path.join(rootDir, 'index');
  
  fs.mkdirSync(indexDir, { recursive: true });
  
  if (!fs.existsSync(dataDir)) {
    console.log('❌ data/ не существует');
    fs.writeFileSync(path.join(indexDir, 'index.bin'), xor('[]'));
    return;
  }
  
  const files = fs.readdirSync(dataDir).filter(f => f.endsWith('.bin'));
  console.log(`Найдено ${files.length} файлов`);
  
  const indexData = [];
  let totalMovies = 0;
  
  for (const file of files) {
    const filePath = path.join(dataDir, file);
    const encrypted = fs.readFileSync(filePath, 'utf8');
    
    try {
      const decrypted = xor(encrypted);
      const data = JSON.parse(decrypted);
      const films = data.films || [];
      
      for (const film of films) {
        indexData.push({
          code: film.code,
          title: film.title,
          releaseDate: film.releaseDate,
          thumbnail: film.thumbnail,
          genres: film.metadata?.genre || [],
          actresses: film.metadata?.actress || []
        });
        totalMovies++;
      }
      
      console.log(`  ${file}: ${films.length} фильмов`);
      
    } catch (err) {
      console.error(`  ❌ ${file}: ${err.message}`);
      
      // Отладка: показываем кусок расшифрованного текста вокруг места ошибки
      const pos = parseInt(err.message.match(/position (\d+)/)?.[1] || '0');
      const decrypted = xor(encrypted);
      console.error(`     Вокруг позиции ${pos}:`);
      console.error(`     ${decrypted.substring(Math.max(0, pos - 50), pos + 50)}`);
      console.error(`     Первые 100 символов: ${decrypted.substring(0, 100)}`);
    }
  }
  
  // Сохраняем индекс
  const indexJSON = JSON.stringify(indexData);
  const encrypted = xor(indexJSON);
  
  fs.writeFileSync(path.join(indexDir, 'index.bin'), encrypted);
  fs.writeFileSync(path.join(indexDir, 'meta.json'), JSON.stringify({
    lastBuild: new Date().toISOString(),
    totalMovies,
    filesCount: files.length
  }));
  
  console.log(`✅ Готово: ${totalMovies} фильмов в индексе`);
}

buildIndex().catch(err => {
  console.error('💥 Ошибка:', err);
  process.exit(1);
});
