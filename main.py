from decouple import config
import requests

# Set up the API key and base URL

base_url = 'https://newsapi.org/v2/top-headlines?country=us'

apiKey = config('api_key')

# Set up the parameters for the API request
parameters = {
    'apiKey': apiKey,
    'category': 'business',
    'pageSize': 10,
    'sortBy': 'popularity'
}

try:
    # Send the GET request to the API
    response = requests.get(base_url, params=parameters)

    # Check if the request was successful
    if response.status_code == 200:
        # Parse the JSON response
        data = response.json()

        # Check if any articles were returned
        if data['totalResults'] > 0:
            articles = data['articles']

            # Print the top 10 articles
            for article in articles:
                print('Title:', article['title'])
                print('Summary:', article['description'])
                print('Source:', article['url'])
                print('---')
        else:
            print('No articles found.')
    else:
        print('Error:', response.status_code)
except requests.exceptions.RequestException as e:
    print('Error:', e)
