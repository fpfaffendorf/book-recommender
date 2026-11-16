# Recomendaciones de libros

Aplicación de escritorio (Tkinter + matplotlib) que le pide a Claude **N veces** la misma
recomendación de 5 libros sobre el tema que elijas. El prompt de ejemplo pide libros en inglés
sobre trading cuantitativo para principiantes en 2026. Guarda cada respuesta en SQLite y
muestra cuáles libros recomienda más y cómo evoluciona la cuenta iteración por iteración.

## Por qué existe

Me pasa seguido que, cuando le pido a una IA recomendaciones de libros, choco con el no
determinismo: las recomendaciones cambian de una ejecución a otra. Otras veces, revisando la cadena
de razonamiento (CoT), encuentro que la IA recomienda desde su conocimiento interno, sin investigar
en internet. Así, las recomendaciones terminan siendo pobres y bastante aleatorias.

Lo que hace esta herramienta, además de darle contexto a la consulta (instrucciones fijas que piden
libros reales, siempre la misma cantidad y los mismos datos de cada uno), es:

1. **Obligar a investigar en internet en vez de responder de memoria.** Le da a Claude la
   herramienta de búsqueda web y le indica que busque recomendaciones actuales en fuentes
   variadas, que compare qué libros se repiten y por qué, y que verifique en editoriales o
   catálogos el ISBN de cada uno. Cada ejecución guarda cuántas búsquedas hizo, qué fuentes citó y
   el informe que escribió, así se puede comprobar que investigó. Si responde sin buscar, la
   ejecución queda marcada con un aviso.
2. **Convertir el no determinismo en un dato.** En vez de quedarse con una sola respuesta, hace la
   misma consulta N veces, cada una independiente, y cuenta cuántas veces aparece cada libro. Los
   libros que aparecen en casi todas las ejecuciones son las recomendaciones sólidas; los que
   aparecen una sola vez son ruido. Los gráficos muestran el ranking, en qué ejecución apareció cada
   libro y cómo evoluciona la cuenta a medida que se suman ejecuciones.
3. **Identificar cada libro sin ambigüedad.** Cada libro se identifica por su ISBN-13, con el
   dígito verificador validado, y las distintas ediciones de una misma obra se pueden contar
   juntas. Un ISBN inválido queda marcado, porque suele ser señal de que el dato se inventó.
4. **Permitir comparar.** Los lotes con y sin búsqueda web se guardan por separado, así podés ver
   cuánto cambian las recomendaciones cuando la IA investiga y cuando responde de memoria. Todo
   queda en una base SQLite local para analizarlo después.

## Instalación

Requiere Python 3.10 o superior (con Tkinter, que viene incluido en Windows).

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Con `uv`: `uv venv` y después `uv pip install -r requirements.txt`.

Configurá tu clave de la API de Anthropic (PowerShell):

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
```

## Uso

```bash
python book-recommender.py
```

1. Elegí el modelo (por defecto `claude-opus-5-5`), el esfuerzo, la cantidad de ejecuciones **N**
   y cuántas llamadas en paralelo hacer.
2. Escribí en el prompt qué libros querés: el tema, el público, el nivel y, si te importa, el idioma.
   Por ejemplo: *"Recomendame libros de divulgación sobre astronomía para adolescentes, en español"*.
   El botón *Restaurar* vuelve al prompt de ejemplo. Después apretá **Ejecutar**.
   El prompt solo contiene el pedido. Las instrucciones de formato (5 libros con título, autor e
   ISBN-13) las agrega la aplicación en cada llamada, en un mensaje de sistema separado, así que
   editar o borrar partes del prompt no rompe la respuesta. Para verlas, usá el botón
   *Ver instrucciones fijas*.
3. Cada ejecución es independiente, sin historial. Las respuestas se guardan a medida que llegan
   y los gráficos se actualizan solos.

### Búsqueda en internet

Con **Buscar en internet** activado (la opción por defecto), Claude no responde de memoria. Cada
ejecución tiene dos pasos:

1. **Investigación.** Claude usa la herramienta de búsqueda web de Anthropic (hasta 10 búsquedas
   por ejecución): busca recomendaciones y reseñas actuales, compara las fuentes, verifica los ISBN
   y escribe un informe con citas.
2. **Extracción.** Una segunda llamada pasa ese informe a JSON (título, autor e ISBN-13), sin
   agregar datos que el informe no tenga.

Se hace en dos llamadas porque la búsqueda web siempre devuelve citas, y la API no permite usar citas
junto con la salida estructurada en JSON. En la pestaña **Ejecuciones** se ven las búsquedas
hechas, las fuentes citadas, el informe y el JSON extraído. Si el modelo respondió sin buscar, la
ejecución queda con un aviso.

**Costo:** cada búsqueda cuesta USD 10 cada 1000 búsquedas, y además los resultados que lee el
modelo cuentan como tokens de entrada. Una ejecución con búsqueda sale bastante más cara que una
sin búsqueda. El resumen de arriba muestra las búsquedas y los tokens acumulados.

Si desactivás la casilla, cada ejecución es una sola llamada que responde con lo que el modelo ya
sabe. Los lotes con y sin búsqueda se guardan por separado, así podés compararlos.

### Vistas

- **Gráfico**, con tres vistas:
  - *Evolución acumulada*: un panel por libro (hasta 12) con cuántas veces fue recomendado hasta
    cada iteración. La diagonal punteada es el máximo posible: un libro sobre esa línea apareció en
    todas las respuestas. Se usan paneles separados porque los libros que el modelo recomienda
    siempre tienen la misma curva y en un único gráfico se taparían.
  - *Presencia por iteración*: matriz libro × iteración; la celda azul indica que ese libro
    apareció en esa iteración.
  - *Ranking total*: en cuántas iteraciones apareció cada libro, con el porcentaje.

  Al pasar el mouse sobre el gráfico se ven los valores. La barra inferior permite hacer zoom y
  guardar el gráfico como PNG.
- **Libros**: tabla con cada libro, sus ISBN, si el ISBN es válido y cuántas veces fue recomendado.
- **Ejecuciones**: historial de llamadas; al elegir una se ve la respuesta completa del modelo.

Los filtros de arriba (*Prompt analizado*, *Lote*, *Contar por*, *Mostrar top*) valen para las
tres pestañas.

### Varios temas en la misma base

Los resultados se agrupan **por prompt y modo**. *Prompt analizado* muestra juntos todos los lotes
que usaron exactamente el mismo texto y el mismo modo (con o sin búsqueda web). Así no se mezclan
los libros de distintos temas, ni las respuestas investigadas con las respuestas de memoria. Al
ejecutar un lote, ese filtro pasa al prompt que acabás de usar. Para acumular iteraciones sobre un
mismo tema, ejecutá varias veces el mismo texto, sin cambiarlo. Con *Lote* podés ver un lote
puntual, y *Todos los prompts* junta todo, aunque mezcle temas.

### Contar por obra o por ISBN

Cada libro se guarda identificado por su **ISBN-13**: se normalizan los ISBN-10 y se verifica el
dígito de control. Es habitual que el modelo dé ISBN distintos para el mismo libro (otra edición,
tapa dura o blanda, o un ISBN inventado). Por eso hay dos formas de contar:

- **Obra (título + autor)**, la opción por defecto: agrupa las ediciones de un mismo libro usando el
  título sin subtítulo ni edición ("2nd edition", "2.ª edición", "edición revisada", etc.) y el
  apellido del primer autor. Una traducción tiene otro título, así que cuenta como otra obra.
- **ISBN**: cuenta cada ISBN por separado.

Si un ISBN no pasa la verificación, se marca como `✗ inválido` y el libro se identifica por título
y autor.

## Base de datos

Por defecto se usa `recomendaciones_libros.db`, en la misma carpeta que el script. Para usar otro
archivo: `python book-recommender.py --db otra.db`.

| Tabla | Contenido |
|---|---|
| `batches` | cada lote de N ejecuciones: modelo, esfuerzo, prompt del usuario, instrucciones fijas que agregó la app (`system_prompt`), si usó búsqueda web (`web_search`), N pedido |
| `runs` | cada ejecución: estado, error o avisos, JSON con los libros (`raw_response`), tokens, modelo que respondió y, con búsqueda web, cantidad de búsquedas, informe (`research_text`) y fuentes citadas (`sources`, JSON) |
| `books` | un registro por libro (clave: ISBN-13 válido), con su `work_key` de obra |
| `recommendations` | qué libros devolvió cada ejecución y en qué posición, tal como los escribió el modelo |

Ejemplo de consulta: cuántas veces apareció cada ISBN.

```sql
SELECT b.title, b.author, b.isbn13, COUNT(*) AS veces
FROM recommendations rc JOIN books b ON b.id = rc.book_id
JOIN runs r ON r.id = rc.run_id AND r.status = 'ok'
GROUP BY b.id ORDER BY veces DESC;
```
