<?php

declare(strict_types=1);

$requestedPath = parse_url($_SERVER['REQUEST_URI'] ?? '/', PHP_URL_PATH) ?: '/';
$requestedFile = realpath('/var/www/html' . $requestedPath);

if (
    $requestedPath !== '/index.php'
    && $requestedFile !== false
    && str_starts_with($requestedFile, '/var/www/html/')
    && is_file($requestedFile)
) {
    return false;
}

$autoLogin = strtolower((string) ($_ENV['ADMINER_AUTOLOGIN'] ?? '1'));
$autoLoginDisabled = in_array($autoLogin, ['0', 'false', 'no', 'off'], true);
$manualLoginRequested = isset($_GET['manual']);
$plainRootRequest = ($_SERVER['REQUEST_METHOD'] ?? 'GET') === 'GET'
    && in_array($requestedPath, ['/', '/index.php'], true)
    && empty($_GET);

if (!$autoLoginDisabled && !$manualLoginRequested && $plainRootRequest) {
    $_SERVER['REQUEST_METHOD'] = 'POST';
    $_POST = [
        'auth' => [
            'driver' => $_ENV['ADMINER_AUTOLOGIN_DRIVER'] ?? 'pgsql',
            'server' => $_ENV['ADMINER_AUTOLOGIN_SERVER'] ?? 'db',
            'username' => $_ENV['ADMINER_AUTOLOGIN_USERNAME'] ?? 'cx',
            'password' => $_ENV['ADMINER_AUTOLOGIN_PASSWORD'] ?? 'cx',
            'db' => $_ENV['ADMINER_AUTOLOGIN_DATABASE'] ?? 'cx',
        ],
    ];
}

require '/var/www/html/index.php';
